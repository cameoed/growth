"""Read-only research allocation using the actual shared ranking boundary.

This estimates where another observation could change ordering, not purchase odds.
The estimates never contribute points to a company and never authorize collection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .ranking import policy_version

DEMAND_DIMENSIONS = frozenset(
    {
        "visual_ad_refresh",
        "creative_purchase_requests",
        "observed_workforce",
        "marketing_appointments",
        "creator_partnerships",
        "recurring_workload",
        "content_obligations",
        "production_pressure",
        "external_capacity",
        "creative_hiring",
        "commercial_timing",
        "campaign_cadence",
        "production_complexity",
    }
)
CONTEXT_DIMENSIONS = frozenset(
    {
        "creative_infrastructure",
        "operating_capacity",
        "capital_access",
        "market_variants",
        "technical_product_surface",
        "search_visibility",
        "seo_visibility",
    }
)


def ranking_context(connection, candidate_id: int, *, fallback_score=0, now=None) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("research reference time requires a timezone")
    now = now.astimezone(UTC)
    result = {
        "score": max(0, min(100, fallback_score)),
        "cutoff": None,
        "fit": None,
        "need": None,
        "access": None,
        "risk": None,
        "projection_current": False,
        "suppressed": False,
    }
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_company_ranking'"
    ).fetchone():
        return result
    version = policy_version()
    boundary = connection.execute(
        "SELECT score FROM engine_company_ranking WHERE policy_version=? AND dirty=0 "
        "AND eligible=1 AND excluded=0 AND valid_until>? AND as_of<=? AND score IS NOT NULL "
        "ORDER BY score DESC,company_key LIMIT 50",
        (version, now.isoformat(), now.isoformat()),
    ).fetchall()
    if boundary:
        result["cutoff"] = boundary[-1][0]
    identity = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_ranking_members'"
    ).fetchone()
    match = (
        "candidate_id=? OR company_key=(SELECT company_key FROM engine_ranking_members "
        "WHERE candidate_id=?)"
        if identity
        else "candidate_id=?"
    )
    row = connection.execute(
        "SELECT score,target_json,assessment_json,dirty,valid_until,policy_version,as_of "
        f"FROM engine_company_ranking WHERE {match} LIMIT 1",
        (candidate_id, candidate_id) if identity else (candidate_id,),
    ).fetchone()
    if row is None:
        return result
    current = (
        row["policy_version"] == version
        and not row["dirty"]
        and row["valid_until"] > now.isoformat()
        and row["as_of"] <= now.isoformat()
    )
    if not current:
        return result
    target = json.loads(row["target_json"])
    opportunity = target.get("opportunity") or {}
    assessment = json.loads(row["assessment_json"])
    result.update(
        score=row["score"] if row["score"] is not None else fallback_score,
        fit=opportunity.get("durable_fit"),
        need=opportunity.get("need"),
        access=opportunity.get("access"),
        risk=opportunity.get("risk"),
        projection_current=True,
        suppressed=assessment.get("readiness") in {"deprioritize", "nurture"},
    )
    return result


def boundary_sensitivity(context: dict, dimensions, *, cached: bool = False) -> float:
    """Bound possible ordering value; no weak proxy or missing approved budget wins."""
    if context.get("suppressed"):
        return 0.0
    dimensions = set(dimensions)
    if not dimensions or dimensions <= {"approved_budget", "decision_authority"}:
        return 0.0
    score, cutoff = context["score"], context.get("cutoff")
    # Without a populated list, rank proximity is unknown; retain a neutral prior.
    proximity = 0.5 if cutoff is None else max(0.05, 1 - abs(score - cutoff) / 35)
    if dimensions & DEMAND_DIMENSIONS:
        merit = context.get("fit")
        relevance = 0.85 if merit is None else 0.3 + 0.7 * min(1, merit / 60)
    elif "buyer_access" in dimensions:
        relevance = 0.8 if score >= (cutoff or 40) else 0.3
    elif dimensions <= CONTEXT_DIMENSIONS:
        relevance = 0.3
    else:
        relevance = 0.55
    # Lower-ranked, evidence-strong companies remain in contention when unresolved
    # need can plausibly close the gap; static abundance cannot create this allowance.
    if (
        dimensions & DEMAND_DIMENSIONS
        and (context.get("fit") or 0) >= 45
        and (context.get("need") is None or context["need"] < 55)
    ):
        proximity = max(proximity, 0.6)
    if context.get("risk") is not None:
        relevance *= max(0.15, 1 - context["risk"] / 120)
    return round(min(1.0, proximity * relevance + (0.1 if cached else 0)), 5)


def resolved_dimensions(observations, registry, company_key: str, *, now=None) -> set[str]:
    now = now or datetime.now(UTC)
    result = set()
    for observation in observations:
        definition = registry.get(observation.scanner_id)
        observed = observation.last_observed_at or observation.first_observed_at
        if (
            definition is None
            or observation.company_key.removeprefix("domain:") != company_key
            or not observation.score_eligible
            or observation.discovery_only
            or observation.validity_status in {"expired", "disputed", "historical", "unknown"}
            or observed > now
            or now - observed > timedelta(seconds=definition.ttl_seconds)
            or (observation.expires_at is not None and observation.expires_at <= now)
        ):
            continue
        result.update(definition.eig_dimensions)
    return result


def _provider_cohort_rows(connection, provider, limit, now, *, details=True,
                          due_only=False, exclude_roots=(), only_roots=None, bulk_selection=False,
                          candidate_ids=None, include_target=True):
    from ..dataforseo_intelligence import seo_registered_domain
    from ..store import (
        TREG_PEOPLE_REFRESH_DAYS,
        TREG_PEOPLE_SCANNER_ID,
        TREG_PEOPLE_SCANNER_VERSION,
    )

    stamp = now.isoformat()
    args = [policy_version(), stamp, stamp]
    limit = max(1, min(int(limit), 100_000 if bulk_selection else 2_500))
    filters = (
        "WHERE r.policy_version=? AND r.dirty=0 AND r.eligible=1 AND r.excluded=0 "
        "AND r.valid_until>? AND r.as_of<=? "
    )
    if provider == "people" and due_only:
        filters += (
            "AND NOT EXISTS(SELECT 1 FROM scanner_executions e WHERE e.company_key=r.company_key "
            "AND e.scanner_id=? AND e.scanner_version=? "
            "AND e.status IN ('success','success_empty') "
            "AND e.recorded_at<=? AND e.started_at<=? "
            "AND COALESCE(e.finished_at,e.started_at)<=? "
            "AND COALESCE(e.finished_at,e.started_at)>?) "
        )
        args.extend([TREG_PEOPLE_SCANNER_ID, TREG_PEOPLE_SCANNER_VERSION, stamp, stamp, stamp,
                     (now - timedelta(days=TREG_PEOPLE_REFRESH_DAYS)).isoformat()])
    if provider == "people" and candidate_ids is not None:
        filters += "AND r.candidate_id IN (SELECT value FROM json_each(?)) "
        args.append(json.dumps(sorted(candidate_ids)))
    fields = (
        "r.company_key,r.candidate_id,r.score,r.policy_version,r.as_of,r.target_json,c.hostname"
    ) if details else "r.company_key"
    if bulk_selection or not include_target:
        fields = fields.replace("r.target_json,", "")
    query = (
        f"SELECT {fields} FROM engine_company_ranking r "
        "INDEXED BY idx_engine_company_ranking_scores JOIN candidates c ON c.id=r.candidate_id "
        + filters + "ORDER BY r.score DESC,r.company_key LIMIT ?"
    )
    if provider == "seo":
        # The first occurrence of a root in exact score/company order is the same
        # representative selected by ROW_NUMBER within that root. Stop only after
        # the requested number of distinct roots; duplicates never consume slots.
        query = (
            "SELECT r.company_key "
            "FROM engine_company_ranking r INDEXED BY idx_engine_company_ranking_scores "
            "JOIN candidates c ON c.id=r.candidate_id " + filters
            + "ORDER BY r.score DESC,r.company_key"
        )
        owned_snapshot = details and not connection.in_transaction
        if owned_snapshot:
            connection.execute("BEGIN")
        cursor = None
        excluded = set(exclude_roots)
        included = set(only_roots) if only_roots is not None else None
        try:
            cursor = connection.execute(query, args)
            rows, seen = [], set()
            for row in cursor:
                root = seo_registered_domain(row["company_key"])
                if (not root or root in seen or root in excluded
                        or (included is not None and root not in included)):
                    continue
                seen.add(root)
                rows.append({**dict(row), "seo_domain": root})
                if len(rows) == limit:
                    break
            if details and rows:
                # Duplicate roots never cause wide JSON decoding. Keep selection and
                # detail reads in one pinned snapshot, including concurrent purges.
                by_key = {}
                for start in range(0, len(rows), 500):
                    keys = [row["company_key"] for row in rows[start:start+500]]
                    placeholders = ",".join("?" for _ in keys)
                    by_key.update((row["company_key"], dict(row)) for row in connection.execute(
                        f"SELECT {fields} FROM engine_company_ranking r "
                        "JOIN candidates c ON c.id=r.candidate_id "
                        f"WHERE r.company_key IN ({placeholders})", keys,
                    ))
                rows = [{**by_key[row["company_key"]], "seo_domain": row["seo_domain"]}
                        for row in rows]
            return rows
        finally:
            if cursor is not None:
                cursor.close()
            if owned_snapshot:
                connection.rollback()  # This helper owns only a read snapshot.
    return connection.execute(query, (*args, limit)).fetchall()


def canonical_provider_company_keys(store, provider, *, limit, now):
    """The exact ranked identities, without loading unused evidence or paid receipts."""
    connection = store.connection
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_ranking_backfill'"
    ).fetchone() or not connection.execute(
        "SELECT 1 FROM engine_ranking_backfill LIMIT 1"
    ).fetchone():
        return None
    if provider not in {"people", "seo"}:
        raise ValueError("unsupported ranking research provider")
    return {row["company_key"] for row in _provider_cohort_rows(
        connection, provider, limit, now, details=False)}


def canonical_provider_candidates(store, provider: str, *, limit: int, now: datetime,
                                  due_only=False, exclude_roots=(), only_roots=None,
                                  bulk_selection=False, candidate_ids=None, include_target=True):
    """Use the canonical cohort after ranking activation; retain pre-migration callers.

    This only selects saved identities and existing provider receipts. It does not
    fetch, resolve emails, reserve a budget, or relax an execution cooldown.
    """
    from ..store import (
        DATAFORSEO_REFRESH_DAYS,
        TREG_PEOPLE_MIN_GOOD_CONTACTS,
        TREG_PEOPLE_REFRESH_DAYS,
        TREG_PEOPLE_SCANNER_ID,
        TREG_PEOPLE_SCANNER_VERSION,
    )

    connection = store.connection
    if (
        not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='engine_ranking_backfill'"
        ).fetchone()
        or not connection.execute("SELECT 1 FROM engine_ranking_backfill LIMIT 1").fetchone()
    ):
        return None
    if provider not in {"people", "seo"}:
        raise ValueError("unsupported ranking research provider")
    rows = _provider_cohort_rows(
        connection, provider, limit, now, due_only=due_only,
        exclude_roots=exclude_roots, only_roots=only_roots, bulk_selection=bulk_selection,
        candidate_ids=candidate_ids, include_target=include_target,
    )
    receipts = {}
    if provider == "seo" and not bulk_selection:
        receipts = store.dataforseo_root_last_gathered(
            [row["seo_domain"] for row in rows], as_of=now,
        )
    elif provider == "people" and not due_only:
        for start in range(0, len(rows), 500):
            keys = [row["company_key"] for row in rows[start:start + 500]]
            placeholders = ",".join("?" for _ in keys)
            saved = connection.execute(
                "SELECT * FROM (SELECT company_key,id,status,"
                "COALESCE(finished_at,started_at) AS gathered_at,observation_count,"
                "ROW_NUMBER() OVER (PARTITION BY company_key ORDER BY "
                "COALESCE(finished_at,started_at) DESC,id DESC) AS position "
                f"FROM scanner_executions WHERE company_key IN ({placeholders}) "
                "AND scanner_id=? AND scanner_version=? AND status IN ('success','success_empty') "
                "AND recorded_at<=? AND started_at<=? AND COALESCE(finished_at,started_at)<=?) "
                "WHERE position=1",
                (*keys, TREG_PEOPLE_SCANNER_ID, TREG_PEOPLE_SCANNER_VERSION,
                 now.isoformat(), now.isoformat(), now.isoformat()),
            ).fetchall()
            receipts.update((row["company_key"], row) for row in saved)
    result = []
    for rank, row in enumerate(rows, 1):
        target = {} if bulk_selection or not include_target else json.loads(row["target_json"])
        opportunity = target.get("opportunity") or {}
        item = {
            "rank": rank,
            "candidate_id": row["candidate_id"],
            "hostname": row["hostname"],
            "canonical_domain": row["company_key"],
            "founder_priority_score": row["score"],
            "durable_fit_score": opportunity.get("durable_fit", 0),
            "contact_now_score": opportunity.get("contact_now", 0),
            "target_score": target.get("total", 0),
            "ranking_policy": row["policy_version"],
            "ranking_as_of": row["as_of"],
        }
        if provider == "people":
            receipt = receipts.get(row["company_key"])
            gathered = datetime.fromisoformat(receipt["gathered_at"]) if receipt else None
            count = receipt["observation_count"] if receipt else 0
            current = gathered is not None and gathered > now - timedelta(
                days=TREG_PEOPLE_REFRESH_DAYS
            )
            due = (
                gathered + timedelta(days=TREG_PEOPLE_REFRESH_DAYS)
                if gathered
                else None
            )
            item.update(
                archetype=(target.get("company_role") or {}).get("role", "unknown"),
                people_execution_id=receipt["id"] if receipt else None,
                people_status=receipt["status"] if receipt else None,
                good_contact_count=count,
                minimum_good_contacts=TREG_PEOPLE_MIN_GOOD_CONTACTS,
                people_current=current,
                people_due=not current,
                last_gathered_at=gathered.isoformat() if gathered else None,
                next_due_at=due.isoformat() if due else None,
            )
        else:
            receipt = receipts.get(row["seo_domain"])
            gathered = datetime.fromisoformat(receipt) if receipt else None
            due = gathered + timedelta(days=DATAFORSEO_REFRESH_DAYS) if gathered else None
            item.update(
                seo_domain=row["seo_domain"],
                last_gathered_at=receipt,
                next_due_at=due.isoformat() if due else None,
                seo_due=due is None or due <= now,
            )
        result.append(item)
    return result
