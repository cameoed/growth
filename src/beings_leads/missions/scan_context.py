"""Provider-free scan preferences from the selected mission's accepted interpretation."""

from ..microlink_mission import mission_terms
from .reads import _score_available, list_companies
from .store import get_mission


def scan_context(connection, mission_id="beings"):
    if mission_id in {"beings", "all"}:
        return {}
    mission = get_mission(connection, mission_id)
    if not _score_available(mission):
        raise ValueError("Complete the selected mission's AI setup before scanning")
    return {
        "mission_id": mission_id,
        "revision": mission["revision"],
        "brief": mission["brief"],
        "search_terms": list(mission_terms(mission_id, mission["spec"])),
        "criteria": mission["spec"],
        "usage": "Research preferences only; verify all company claims with owned evidence.",
    }


def ranked_candidate_ids(store, mission_id, limit):
    """Use the mission's existing score order; never substitute Beings buyer scores."""
    from ..sales_engine.service import SalesEngineService

    scan_context(store.connection, mission_id)
    engine = SalesEngineService(store, initialize=False)
    rows = list_companies(engine, mission_id, limit=limit)["items"]
    return [row["candidate_id"] for row in rows if row.get("candidate_id") is not None]


def source_preference(suggestion, context):
    """Stable relevance ordering only; all operator-selected sources remain eligible."""
    text = " ".join(str(getattr(suggestion, field, "")) for field in (
        "name", "url", "discovery_mechanism", "why_relevant", "category",
    )).casefold()
    return sum(term.casefold() in text for term in context.get("search_terms", []))
