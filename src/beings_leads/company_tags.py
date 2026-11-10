"""Exact operator tag membership across canonical company aliases."""
from .store import _normalize_tags


def company_tag_ids(store, candidate_id):
    row = store.connection.execute(
        "SELECT hostname FROM candidates WHERE id=?", (candidate_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Company not found")
    member = store.connection.execute(
        "SELECT company_key FROM engine_mission_members WHERE candidate_id=?", (candidate_id,),
    ).fetchone()
    key = member[0] if member else row[0]
    return sorted({candidate_id, *(r[0] for r in store.connection.execute(
        "SELECT candidate_id FROM engine_mission_members WHERE company_key=?", (key,),
    ))})


def company_tags(store, candidate_id):
    tags = set()
    for cid in company_tag_ids(store, candidate_id):
        tags.update(r[0] for r in store.connection.execute(
            "SELECT tag FROM prospect_tags WHERE candidate_id=?", (cid,),
        ))
    return sorted(tags)


def change_company_tag(store, candidate_id, tag, action):
    from datetime import UTC, datetime

    from .sales_engine.persistence import SalesEngineRepository

    tag = normalize_tag(tag)
    if not tag:
        raise ValueError("Enter a tag")
    if action not in {"add", "remove"}:
        raise ValueError("Unknown tag action")
    with SalesEngineRepository(store.connection)._transaction():
        ids = company_tag_ids(store, candidate_id)
        if action == "add":
            tags = company_tags(store, candidate_id)
            if tag not in tags and len(tags) >= 20:
                raise ValueError("A company can have at most 20 tags")
            store.connection.execute(
                "INSERT OR IGNORE INTO prospect_tags(candidate_id,tag,created_at) VALUES(?,?,?)",
                (candidate_id, tag, datetime.now(UTC).isoformat()),
            )
        else:
            for cid in ids:
                store.connection.execute(
                    "DELETE FROM prospect_tags WHERE candidate_id=? AND tag=?", (cid, tag),
                )
        return {"candidate_id": candidate_id, "tags": company_tags(store, candidate_id)}


def normalize_tag(value):
    if len(value) > 40:
        raise ValueError("Tags must be at most 40 characters")
    tags = _normalize_tags([value])
    return tags[0] if tags else ""


def tag_condition(company_key, *, mission=False):
    # Expressions are internal SQL identifiers, never user input.
    members = "engine_mission_members" if mission else "engine_ranking_members"
    return (
        f"{company_key} IN (SELECT COALESCE(tm.company_key,tc.hostname) "
        f"FROM prospect_tags tt JOIN candidates tc ON tc.id=tt.candidate_id "
        f"LEFT JOIN {members} tm ON tm.candidate_id=tc.id WHERE tt.tag=?)"
    )


def tagged_candidates(store, tag):
    tag = normalize_tag(tag)
    if not tag:
        raise ValueError("Select a tag")
    rows = store.connection.execute(
        "SELECT c.id,c.hostname FROM candidates c JOIN prospect_tags t "
        "ON t.candidate_id=c.id WHERE t.tag=? ORDER BY c.id", (tag,),
    ).fetchall()
    if not rows:
        raise LookupError("Tag has no companies")
    return rows


def rescore_tag(store, tag, mission_id="beings", *, candidate_ids=None):
    """Publish this exact cohort directly, without providers or the global queue."""
    from .desktop_ranking.control import control_for
    from .missions.scoring import refresh
    from .missions.store import get_mission
    from .sales_engine.persistence import SalesEngineRepository
    from .sales_engine.ranking import refresh_candidate_rankings
    from .sales_engine.service import SalesEngineService

    SalesEngineService(store)
    mission_id = "beings" if mission_id == "all" else mission_id
    mission = get_mission(store.connection, mission_id)
    if mission_id != "beings" and (
        mission["spec"].get("compiler_pending") or mission["spec"].get("compiler_error")
    ):
        raise ValueError("Compile this mission before re-scoring")
    rows = tagged_candidates(store, tag) if candidate_ids is None else [
        row for cid in dict.fromkeys(candidate_ids)
        if (row := store.connection.execute(
            "SELECT id,hostname FROM candidates WHERE id=?", (cid,),
        ).fetchone()) is not None
    ]
    members = "engine_ranking_members" if mission_id == "beings" else "engine_mission_members"
    keys = {}
    for row in rows:
        if mission_id == "beings":
            # A newly imported observation may precede the background identity walk.
            with SalesEngineRepository(store.connection)._transaction():
                store.connection.execute(
                    "INSERT INTO engine_ranking_members(candidate_id,company_key,snapshot_id) "
                    "SELECT s.candidate_id,t.canonical_domain,s.id FROM snapshots s "
                    "JOIN snapshot_target_intelligence t ON t.snapshot_id=s.id "
                    "WHERE s.candidate_id=? ORDER BY s.id DESC LIMIT 1 "
                    "ON CONFLICT(candidate_id) DO UPDATE SET company_key=excluded.company_key,"
                    "snapshot_id=excluded.snapshot_id "
                    "WHERE excluded.snapshot_id>=engine_ranking_members.snapshot_id", (row["id"],),
                )
        for member in store.connection.execute(
            f"SELECT company_key FROM {members} WHERE candidate_id=?", (row["id"],),
        ):
            keys.setdefault(member[0], row["id"])
    completed, errors = 0, []
    if mission_id != "beings":
        from .missions.compute import MissionComputePool

        pool = MissionComputePool()
        try:
            ordered = sorted(keys)
            for start in range(0, len(ordered), 500):
                result = refresh(
                    store, mission_id=mission_id, company_keys=ordered[start:start + 500],
                    force=True, limit=500, compute_pool=pool,
                )
                completed += result["refreshed"]
                errors.extend(result["errors"])
                if result["discarded"]:
                    errors.append({"error": "Evidence changed; retry re-scoring",
                                   "discarded": result["discarded"]})
        finally:
            pool.close()
    elif control_for(store) is not None:
        completed = refresh_candidate_rankings(store, keys.values())
    else:
        for key, cid in keys.items():
            try:
                completed += refresh_candidate_rankings(store, [cid])
            except Exception as exc:
                errors.append({"company_key": key, "error": type(exc).__name__})
    return {"tag": normalize_tag(tag), "mission_id": mission_id,
            "selected": len(keys), "refreshed": completed, "errors": errors,
            "unscanned": len(rows) - sum(
                bool(store.connection.execute(
                    "SELECT 1 FROM snapshots WHERE candidate_id=? LIMIT 1", (r["id"],),
                ).fetchone()) for r in rows), "provider_requests": 0}
