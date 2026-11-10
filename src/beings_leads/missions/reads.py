"""Exact mission list/detail reads; no synchronous scoring, synthesis or providers."""

import csv
import io
import json
import os
from datetime import UTC, datetime

from ..microlink_hydration import public_contacts
from ..sales_engine import list_cache
from ..sales_engine.ranking import policy_version
from ..sales_engine.ranking_modes import projection_from
from ..sales_engine.score_breakdown import mission_breakdown
from ..sales_engine.summaries import (
    _cache_key,
    _columns,
    _date,
    _identity,
    _list_deadline,
    _snapshot,
    _visible_row,
    company_summary,
)
from . import read_indexes
from .contacts import select_contacts
from .spec import VERSION
from .store import get_mission


def _score_available(mission):
    return (
        mission["id"] != "all"
        and not mission["spec"].get("compiler_pending")
        and not mission["spec"].get("compiler_error")
    )


def _selection(connection, mission_id, now):
    mission = (
        get_mission(connection, mission_id)
        if mission_id != "all"
        else {"id": "all", "revision": 0, "name": "All companies", "spec": {}}
    )
    current = (
        "s.revision=? AND s.policy_version=? AND s.input_version=i.input_version "
        "AND s.as_of<=? AND s.valid_until>?"
    )
    args = [mission["revision"], VERSION, now.isoformat(), now.isoformat()]
    if not _score_available(mission):
        current, args = "0", []
    return mission, current, args


def _filters(connection, search, saved, stage, optimized, tag=""):
    from ..company_tags import normalize_tag, tag_condition
    where, params = read_indexes.search_filter(connection, search, optimized=optimized)
    if tag:
        where += " AND " + tag_condition("i.company_key", mission=True)
        params.append(normalize_tag(tag))
    if saved:
        where += (
            " AND EXISTS(SELECT 1 FROM sales_accounts a WHERE "
            "a.website_host=i.company_key AND (?='' OR a.lifecycle=?))"
        )
        params.extend([stage, stage])
    return where, params


def _visible(row, saved):
    item = {
        "candidate_id": row["candidate_id"],
        "company_key": row["company_key"],
        "company_name": row["company_name"],
        "sales_priority": {
            "score": row["current_score"],
            "version": row["published_version"] or VERSION,
        },
        "priority": row["current_score"],
        "as_of": row["as_of"],
        "ranking_state": "current" if row["as_of"] else "pending",
        "valid_until": row["valid_until"],
        "evidence_version": row["input_version"],
        "published_as_of": row["published_as_of"],
        "published_valid_until": row["published_valid_until"],
    }
    if saved or row["candidate_id"] is None:
        item["account_id"] = row["account_id"]
    return item


def _selection_columns(current, saved):
    return (
        "SELECT i.company_key,i.company_name,i.candidate_id,i.input_version,"
        "s.score AS current_score,s.policy_version AS published_version,"
        "s.as_of AS published_as_of,s.valid_until AS published_valid_until,"
        f"CASE WHEN {current} THEN s.as_of END AS as_of,"
        f"CASE WHEN {current} THEN s.valid_until END AS valid_until,"
        f"CASE WHEN {int(bool(saved))} OR i.candidate_id IS NULL THEN "
        "(SELECT id FROM sales_accounts WHERE website_host=i.company_key "
        "ORDER BY id DESC LIMIT 1) END AS account_id "
    )


def _all_projection(connection, now, optimized):
    indexed = optimized and list_cache.state(connection) is not None
    table = "engine_company_listing" if indexed else "engine_company_ranking"
    stamp = now.isoformat()
    current = "s.policy_version=? AND s.dirty=0 AND s.as_of<=? AND s.valid_until>?"
    return table, current, [policy_version(), stamp, stamp]


def _ordered_queries(connection, mission, now, where, params, saved, optimized):
    if mission["id"] == "all":
        table, current, args = _all_projection(connection, now, optimized)
        select = _selection_columns("1", saved)
        inventory = (
            f" FROM engine_mission_inventory i LEFT JOIN {table} s ON s.company_key=i.company_key "
            f"AND {current} WHERE "
        )
        if table == "engine_company_listing" and where == "1":
            # Use the existing score index for the ordinary first page. Page into the
            # inventory's domain-ordered remainder only after all numeric results.
            return [
                (select + "FROM engine_company_listing s INDEXED BY idx_engine_listing_score "
                 "CROSS JOIN engine_mission_inventory i ON i.company_key=s.company_key "
                 f"WHERE {current} AND s.score IS NOT NULL ORDER BY s.score DESC,s.company_key",
                 args),
                (select + inventory + "s.score IS NULL ORDER BY i.company_key", args),
            ]
        return [
            (select + inventory + where + " ORDER BY current_score DESC,i.company_key",
             [*args, *params])
        ]
    if not _score_available(mission):
        select = (
            "SELECT i.*,NULL AS current_score,NULL AS published_version,"
            "NULL AS published_as_of,NULL AS published_valid_until,NULL AS as_of,"
            "NULL AS valid_until,(SELECT id FROM sales_accounts WHERE website_host=i.company_key "
            "ORDER BY id DESC LIMIT 1) AS account_id FROM engine_mission_inventory i WHERE "
        )
        return [(select + where + " ORDER BY i.company_key", params)]
    _, current, args = _selection(connection, mission["id"], now)
    table = "engine_mission_listing" if optimized else "engine_mission_scores"
    if optimized:
        current = current.replace("s.input_version=i.input_version", "s.dirty=0")
    select = _selection_columns(current, saved)
    if not optimized or where.startswith("i.company_key IN "):
        # A complete indexed search cohort drives its own join/sort. For a rare
        # alias, walking the entire score index would discard nearly every row.
        order = "current_score DESC,i.company_key"
        order_args = []
        if not saved:
            order = f"CASE WHEN s.score IS NOT NULL AND {current} THEN 0 ELSE 1 END," + order
            order_args = args
        return [
            (
                select
                + "FROM engine_mission_inventory i LEFT JOIN "
                + table
                + " s ON s.company_key=i.company_key AND s.mission_id=? WHERE "
                + where
                + " ORDER BY " + order,
                [*args, *args, mission["id"], *params, *order_args],
            )
        ]
    # The score index produces every published numeric result in exact order. The
    # second branch merges all null/missing results by company key, without a full sort.
    numeric = (
        select + "FROM engine_mission_listing s INDEXED BY idx_mission_listing_order "
        "CROSS JOIN engine_mission_inventory i ON i.company_key=s.company_key "
        "WHERE s.mission_id=? AND s.score IS NOT NULL AND "
        + where
    )
    score_order = " ORDER BY s.score DESC,s.company_key"
    numeric_args = [*args, *args, mission["id"], *params]
    scored = [(numeric + score_order, numeric_args)] if saved else [
        (numeric + f" AND ({current})" + score_order, [*numeric_args, *args]),
        (numeric + f" AND NOT ({current})" + score_order, [*numeric_args, *args]),
    ]
    unknown = (
        select + "FROM engine_mission_inventory i LEFT JOIN engine_mission_listing s "
        "ON s.company_key=i.company_key AND s.mission_id=? WHERE s.score IS NULL AND "
        + where
        + " ORDER BY i.company_key"
    )
    return [
        *scored,
        (unknown, [*args, *args, mission["id"], *params]),
    ]


def _page_queries(connection, queries, limit, offset):
    rows = []
    for index, (query, params) in enumerate(queries):
        selected = connection.execute(
            query + " LIMIT ? OFFSET ?", [*params, limit - len(rows), offset]
        ).fetchall()
        rows.extend(selected)
        if len(rows) >= limit:
            break
        # Only a crossing/arbitrary deep page needs the preceding branch's exact count.
        if offset and index + 1 < len(queries):
            count = connection.execute("SELECT COUNT(*) FROM (" + query + ")", params).fetchone()[0]
            offset = max(0, offset - count)
    return rows


def _read_revision(connection, mission_id):
    mission_revision = read_indexes.generation(connection)
    if mission_id != "all":
        return mission_revision
    ranking_revision = list_cache.state(connection)
    if ranking_revision is not None:
        return mission_revision, ranking_revision, policy_version()
    return None


def _cohort_stats(engine, mission_id, *, tag, search, saved, stage, now, optimized, cache):
    connection = engine.connection
    key = None
    revision = None
    if cache:
        key = _cache_key(
            engine.store, ("mission-cohort", mission_id, search, saved, stage, VERSION)
        )
        revision = _read_revision(connection, mission_id)
        cached = list_cache.get(key, revision, now)
        if cached is not None:
            return cached
    payload = _compute_stats(
        engine, mission_id, search=search, tag=tag, saved=saved, stage=stage,
        now=now, optimized=optimized
    )
    if key is not None:
        list_cache.put(key, revision, now, _date(payload["valid_until"]), payload)
    return payload


def _compute_stats(engine, mission_id, *, tag, search, saved, stage, now, optimized):
    connection = engine.connection
    mission, current, args = _selection(connection, mission_id, now)
    where, params = _filters(connection, search, saved, stage, optimized, tag)
    total = connection.execute(
        "SELECT COUNT(*) FROM engine_mission_inventory i WHERE " + where, params
    ).fetchone()[0]
    current_count = 0
    if mission_id == "all":
        table, canonical, score_args = _all_projection(connection, now, optimized)
        current_count = connection.execute(
            f"SELECT COUNT(*) FROM {table} s WHERE {canonical} AND EXISTS("
            "SELECT 1 FROM engine_mission_inventory i WHERE i.company_key=s.company_key)",
            score_args,
        ).fetchone()[0]
    elif _score_available(mission):
        if optimized:
            current = current.replace("s.input_version=i.input_version", "s.dirty=0")
            count_query = "SELECT COUNT(*) FROM engine_mission_listing s WHERE s.mission_id=? AND "
        else:
            count_query = (
                "SELECT COUNT(*) FROM engine_mission_inventory i JOIN engine_mission_scores s "
                "ON s.company_key=i.company_key WHERE s.mission_id=? AND "
            )
        current_count = connection.execute(count_query + current, [mission_id, *args]).fetchone()[0]
    inventory_total = connection.execute(
        "SELECT COUNT(*) FROM engine_mission_inventory"
    ).fetchone()[0]
    expires = _list_deadline(now)
    if mission_id == "all":
        expires = list_cache.deadline(connection, now, expires)
    else:
        expires = read_indexes.deadline(
            connection,
            mission_id,
            now,
            expires,
            table="engine_mission_listing" if optimized else "engine_mission_scores",
        )
    return {
        "total": total,
        "coverage": {
            "reviewed_locally": current_count,
            "inventory_total": inventory_total,
            "more_may_exist": mission_id != "all" and current_count < inventory_total,
        },
        "valid_until": expires.isoformat(),
    }


def _uncached_companies(
    engine, mission_id, *, tag, search, limit, offset, saved, stage, now, optimized, cache=False
):
    connection = engine.connection
    mission, _, _ = _selection(connection, mission_id, now)
    stats = _cohort_stats(
        engine,
        mission_id,
        search=search, tag=tag,
        saved=saved,
        stage=stage,
        now=now,
        optimized=optimized,
        cache=cache,
    )
    where, params = _filters(connection, search, saved, stage, optimized, tag)
    queries = _ordered_queries(connection, mission, now, where, params, saved, optimized)
    items = [_visible(row, saved) for row in _page_queries(connection, queries, limit, offset)]
    return {
        "items": items,
        "total": stats["total"],
        "limit": limit,
        "offset": offset,
        "mission_id": mission_id,
        "scope": "shared_inventory",
        "mission_revision": mission["revision"],
        "coverage": stats["coverage"],
        "valid_until": min(
            [stats["valid_until"], *[i["valid_until"] for i in items if i["valid_until"]]]
        ),
        "provider_requests": 0,
    }


def list_companies(
    engine,
    mission_id,
    *,
    search="",
    tag="",
    limit=30,
    offset=0,
    saved=False,
    stage="",
    reference_time=None,
    _optimized=True,
):
    now = reference_time or datetime.now(UTC)
    connection = engine.connection
    optimized = _optimized and read_indexes.ready(connection)
    kwargs = dict(
        search=search, tag=tag,
        limit=limit,
        offset=offset,
        saved=saved,
        stage=stage,
        now=now,
        optimized=optimized,
    )
    if tag or not optimized or not getattr(engine, "store", None) or connection.in_transaction:
        with _snapshot(connection):
            return _with_compilation(connection, mission_id,
                                     _uncached_companies(engine, mission_id, **kwargs))
    key = _cache_key(
        engine.store, ("mission", mission_id, search, limit, offset, saved, stage, VERSION)
    )
    with list_cache.flight(key, getattr(engine, "_read_cancel", None)), _snapshot(connection):
        # Verify the mission on every checkout: archive/deletion cannot leak cached results.
        _selection(connection, mission_id, now)
        revision = _read_revision(connection, mission_id)
        if revision is None:
            return _uncached_companies(engine, mission_id, **kwargs)
        cached = list_cache.get(key, revision, now)
        if cached is not None:
            return _with_compilation(connection, mission_id, cached)
        payload = _uncached_companies(engine, mission_id, **kwargs, cache=True)
        list_cache.put(key, revision, now, _date(payload["valid_until"]), payload)
        return _with_compilation(connection, mission_id, payload)


def newest_companies(engine, mission_id, *, search, tag, limit, offset, prospects_only, mode):
    """Page canonical discoveries, then read published scores for just that page."""
    connection = engine.connection
    now = datetime.now(UTC)
    with _snapshot(connection):
        mission, current, score_args = _selection(connection, mission_id, now)
        where, args = _filters(connection, search, False, "", read_indexes.ready(connection), tag)
        where = " WHERE i.candidate_id IS NOT NULL AND " + where
        if prospects_only:
            where += (
                " AND NOT EXISTS(SELECT 1 FROM sales_accounts a WHERE a.website_host=i.company_key)"
            )
        inventory = " FROM engine_mission_inventory i"
        total = connection.execute("SELECT COUNT(*)" + inventory + where, args).fetchone()[0]
        if read_indexes.discovery_order_ready(connection):
            source = (
                " FROM engine_mission_discovery_order d JOIN engine_mission_inventory i "
                "ON i.company_key=d.company_key"
            )
            first_date, company_key = "d.first_discovered_at", "d.company_key"
        else:
            source = inventory
            first_date = (
                "(SELECT MIN(c.discovered_at) FROM engine_mission_members m "
                "JOIN candidates c ON c.id=m.candidate_id WHERE m.company_key=i.company_key)"
            )
            company_key = "i.company_key"
        rows = connection.execute(
            f"SELECT i.candidate_id,i.company_key,i.company_name,i.snapshot_id,"
            f"{first_date} AS first_discovered_at"
            + source + where + f" ORDER BY first_discovered_at DESC,{company_key} LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        published = {}
        if rows:
            keys = [row["company_key"] for row in rows]
            placeholders = ",".join("?" for _ in keys)
            if mission_id in {"beings", "all"}:
                ranking_mode = "balanced" if mission_id == "all" else mode
                scores = connection.execute(
                    f"SELECT {_columns(ranking_mode)} FROM {projection_from(ranking_mode)} "
                    f"WHERE r.company_key IN ({placeholders})", keys,
                )
                published = {
                    row["company_key"]: _visible_row(row, now, ranking_mode) for row in scores
                }
            elif _score_available(mission):
                scores = connection.execute(
                    _selection_columns(current, False)
                    + "FROM engine_mission_inventory i JOIN engine_mission_scores s "
                    "ON s.company_key=i.company_key AND s.mission_id=? "
                    f"WHERE i.company_key IN ({placeholders})",
                    [*score_args, *score_args, mission_id, *keys],
                )
                published = {row["company_key"]: _visible(row, False) for row in scores}
        items = [
            {
                "sales_priority": {"score": None}, "valid_until": None, "as_of": None,
                "ranking_state": "pending" if row["snapshot_id"] else "uninspected",
                **published.get(row["company_key"], {}),
                "candidate_id": row["candidate_id"], "company_key": row["company_key"],
                "company_name": row["company_name"],
                "first_discovered_at": row["first_discovered_at"],
            }
            for row in rows
        ]
    return {
        "items": items, "total": total, "limit": limit, "offset": offset,
        "mission_id": mission_id, "mission_revision": mission["revision"],
        "valid_until": min([
            _list_deadline(now).isoformat(),
            *(i["valid_until"] for i in items if i["valid_until"]),
        ]),
        "provider_requests": 0,
    }


def _with_compilation(connection, mission_id, payload):
    if mission_id == "all":
        return payload
    from ..sales_engine.maintenance import ranking_worker_state
    from .compiler import status

    mission = get_mission(connection, mission_id)
    # Compiler requests can change without an inventory generation change. Read
    # their small status after every cache checkout, in the same WAL snapshot.
    return {**payload, "scoring_available": _score_available(mission),
            **ranking_worker_state(connection, mission_id),
            "compilation": status(connection, mission)}


def _last_review_cost(connection, company_key, mission_id):
    row = connection.execute(
        """SELECT actual_microusd, status FROM engine_budget_reservations
        WHERE company_key=? AND purpose=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (company_key, f"Mission semantic review {mission_id}"),
    ).fetchone()
    return dict(row) if row else None


def summary(engine, mission_id, candidate_id=None, *, account_id=None):
    now = datetime.now(UTC)
    connection = engine.connection
    with _snapshot(connection):
        mission, current, args = _selection(connection, mission_id, now)
        score = "s.score" if _score_available(mission) else "NULL"
        if account_id is not None:
            account = connection.execute(
                "SELECT website_host FROM sales_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise LookupError("Saved company not found")
            key = account[0]
        else:
            identity = _identity(connection, candidate_id)
            key = identity["company_key"]
        licensed = engine.store.treg_people_intelligence(key, as_of=now)
        base, _ = company_summary(
            engine, candidate_id, account_id=account_id, reference_time=now, _licensed=licensed
        )
        if mission_id == "all":
            row = connection.execute(
                "SELECT i.*,r.valid_until AS ranking_valid_until FROM engine_mission_inventory i "
                "LEFT JOIN engine_company_ranking r ON r.company_key=i.company_key "
                "WHERE i.company_key=?", (key,)
            ).fetchone()
        else:
            row = connection.execute(
                f"""SELECT i.*,{score} AS current_score,s.policy_version AS published_version,
            s.as_of AS published_as_of,s.valid_until AS published_valid_until,
            CASE WHEN {current} THEN s.as_of END AS as_of,
            CASE WHEN {current} THEN s.valid_until END AS valid_until
            FROM engine_mission_inventory i
            LEFT JOIN engine_mission_scores s ON s.company_key=i.company_key AND s.mission_id=?
            WHERE i.company_key=?""",
                [*args, *args, mission_id, key],
            ).fetchone()
        if row is None:
            raise LookupError("Company not found")
        # Reuse the unchanged contact safety/identity/freshness path, then rank original
        # accepted identities by this mission's functions instead of Beings title relevance.
        public = {}
        observed = None
        if row["snapshot_id"]:
            source = connection.execute(
                "SELECT json_extract(observation_json,'$.contacts'),observed_at "
                "FROM snapshots WHERE id=?",
                (row["snapshot_id"],),
            ).fetchone()
            public = json.loads(source[0] or "{}")
            observed = source[1]
        contacts = select_contacts(
            key, public_contacts(connection, key, public, now=now), licensed, observed, now,
            mission["spec"].get("contact_terms", []),
        )
        if mission_id == "all":
            return {
                **base,
                "company_name": row["company_name"],
                "mission_id": mission_id,
                "mission_revision": mission["revision"],
                "score_breakdown": None,
                "contact_recommendations": contacts,
                "mission_review": {"configured": False},
                "published_as_of": base["as_of"],
                "published_valid_until": row["ranking_valid_until"] if base["as_of"] else None,
            }
        valid = bool(row["as_of"]) and _score_available(mission)
        return {
            **base,
            "company_name": row["company_name"],
            "mission_id": mission_id,
            "mission_revision": mission["revision"],
            "sales_priority": {
                "score": row["current_score"],
                "version": row["published_version"] or VERSION,
            },
            "score_breakdown": mission_breakdown(connection, mission_id, key)
            if row["current_score"] is not None
            else None,
            "contact_recommendations": contacts,
            "mission_review": {
                "last_cost": _last_review_cost(connection, key, mission_id),
                "configured": _score_available(mission)
                and row["candidate_id"] is not None
                and bool(os.getenv("OPENAI_API_KEY"))
            },
            "ranking_state": "current" if valid else "pending",
            "evidence_version": row["input_version"],
            "as_of": row["as_of"] if valid else None,
            "published_as_of": row["published_as_of"],
            "published_valid_until": row["published_valid_until"],
            "valid_until": min(base["valid_until"], row["valid_until"])
            if valid
            else base["valid_until"],
        }


def export_csv(engine, mission_id, *, search="", saved=False):
    """One ordered, complete inventory cursor in one consistent read snapshot."""
    now = datetime.now(UTC)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["mission_id", "company", "domain", "sales_priority", "candidate_id"])
    with _snapshot(engine.connection):
        mission, _, _ = _selection(engine.connection, mission_id, now)
        optimized = read_indexes.ready(engine.connection)
        where, params = _filters(engine.connection, search, saved, "", optimized)
        queries = _ordered_queries(engine.connection, mission, now, where, params, saved, optimized)
        for query, args in queries:
            for row in engine.connection.execute(query, args):
                name = row["company_name"]
                if name.startswith(("=", "+", "-", "@", "\t", "\r")):
                    name = "'" + name
                writer.writerow(
                    [
                        mission_id,
                        name,
                        row["company_key"],
                        row["current_score"],
                        row["candidate_id"],
                    ]
                )
    return output.getvalue()
