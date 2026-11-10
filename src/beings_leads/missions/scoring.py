"""Local mission ranking over exhaustive saved evidence; no network or AI calls."""

import hashlib
import json
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from itertools import islice, repeat, zip_longest
from urllib.parse import urlsplit

from ..evidence_policy import durable_weight
from ..models import SiteObservation
from ..sales_engine.evidence_accounting import ledger_where
from ..sales_engine.persistence import SalesEngineRepository
from ..storage_json import encode_text
from ..value_utils import timezone_datetime as _date
from .activity_catalog import ACTIVITY_ALIASES
from .bridge import own as _own
from .compute import INVENTORY_PIN_SQL
from .contacts import ACCESS_FACT, access_evidence
from .context import matches as _matches
from .context import topic_records
from .spec import VERSION
from .store import get_mission

BACKGROUND_BATCH_SIZE = 1_500
LARGE_BACKGROUND_BATCH_SIZE = 3_000
MISSIONS_PER_COMPANY_BATCH = 4


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return ""


def _future_boundaries(value, now):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_at") and (stamp := _date(child)) and stamp > now:
                yield stamp
            yield from _future_boundaries(child, now)
    elif isinstance(value, list):
        for child in value:
            yield from _future_boundaries(child, now)


def _ledger_metadata(store):
    """Cache schema metadata only, invalidated by SQLite's transaction-visible schema version."""
    from ..sales_engine.evidence_accounting import LEDGERS

    prefetch = getattr(store.connection, "prefetch", None)
    tables_sql = "SELECT name FROM sqlite_master WHERE type='table'"
    if prefetch is not None:
        prefetch((("PRAGMA schema_version", ()), ("PRAGMA query_only", ()), (tables_sql, ())))
    version = store.connection.execute("PRAGMA schema_version").fetchone()[0]
    # A writable caller may roll back DDL and later reuse the same version for
    # different DDL. Never cache or reuse metadata from its open transaction.
    cacheable = not store.connection.in_transaction or bool(
        store.connection.execute("PRAGMA query_only").fetchone()[0]
    )
    cached = getattr(store, "_mission_ledger_schema", None)
    if cached is not None and cached[0] == version:
        return cached[1], cached[2]
    tables = {r[0] for r in store.connection.execute(tables_sql)}
    result = [(table, role) for table, role in LEDGERS.items()
              if table in tables and table not in {"atomic_observations", "evidence_artifacts"}]
    if prefetch is not None:
        prefetch((f"PRAGMA table_info({table})", ()) for table, _ in result)
    column_map = {}
    for table, _ in result:
        columns = {r[1] for r in store.connection.execute(f"PRAGMA table_info({table})")}
        column_map[table] = columns
    if cacheable:
        store._mission_ledger_schema = (version, result, column_map)
    return result, column_map


def _ledger_tables(store):
    return _ledger_metadata(store)[0]


def _ledger_query(table, columns, company):
    """Complete identity-bound accounting, including directional and candidate-linked ledgers."""
    keys = [
        key
        for key in [
            "company_key",
            "subject_company_key",
            "object_company_key",
            "website_host",
            "landing_domain",
            "direct_company_key",
            "domain",
        ]
        if key in columns
    ]
    conditions, args = [f"{key}=?" for key in keys], [company] * len(keys)
    for key in ["subject_key", "object_key"]:
        if key in columns:
            conditions.append(f"{key} IN (?,?)")
            args.extend([company, "domain:" + company])
    ids = "SELECT candidate_id FROM engine_mission_members WHERE company_key=?"
    for key in ["candidate_id", "direct_candidate_id"]:
        if key in columns:
            conditions.append(f"{key} IN ({ids})")
            args.append(company)
    if "account_id" in columns:
        conditions.append(
            f"account_id IN (SELECT id FROM sales_accounts WHERE website_host=? UNION "
            f"SELECT id FROM sales_accounts WHERE candidate_id IN ({ids}))"
        )
        args.extend([company, company])
    if table == "market_events":
        conditions.append(
            "mention_id IN (SELECT id FROM market_entity_mentions WHERE company_key=?)"
        )
        args.append(company)
    if table == "market_documents":
        conditions.extend(
            [
                "id IN (SELECT document_id FROM market_entity_mentions WHERE company_key=?)",
                "id IN (SELECT document_id FROM market_events WHERE direct_company_key=?)",
            ]
        )
        args.extend([company, company])
    if table == "ad_discovery_runs":
        conditions.append(
            f"id IN (SELECT run_id FROM candidate_ad_observations WHERE candidate_id IN ({ids}))"
        )
        args.append(company)
    if table in {"ad_creatives", "advertiser_entities"}:
        field = "advertiser_id" if table == "ad_creatives" else "id"
        conditions.append(
            f"{field} IN (SELECT advertiser_id FROM advertiser_domain_links "
            f"WHERE candidate_id IN ({ids}))"
        )
        args.append(company)
    if not conditions:
        return None
    where = ledger_where(table, conditions)
    return f"SELECT rowid,* FROM {table} WHERE {where} ORDER BY rowid", args


def _ledger_rows(connection, table, columns, company, *, raw=False):
    query = _ledger_query(table, columns, company)
    if query is None:
        return []
    cursor = connection.cursor()
    if raw:
        cursor.row_factory = None
    return cursor.execute(*query)


@contextmanager
def _saved_ledger_packet(connection, ledgers, columns, company):
    read_many = getattr(connection, "read_many", None)
    if read_many is None:
        yield (
            (table, role, _ledger_rows(connection, table, columns[table], company))
            for table, role in ledgers
        )
        return
    queries = [
        (table, role, query)
        for table, role in ledgers
        if (query := _ledger_query(table, columns[table], company)) is not None
    ]
    with read_many([query for _, _, query in queries]) as cursors:
        yield ((table, role, cursor) for (table, role, _), cursor in zip(
            queries, cursors, strict=True,
        ))


def load_evidence(store, company, now, *, ledger_packet=None):
    """Every same-company snapshot/frontier receipt is accounted; no display caps."""
    records, audit = [], []
    boundaries = [now + timedelta(hours=12)]

    def add(
        key,
        source,
        text,
        stamp,
        *,
        eligible=True,
        expires=None,
        root=None,
        heading="",
        fact=None,
        logical_key=None,
    ):
        observed = _date(stamp)
        expiry = _date(expires)
        weight = durable_weight(observed, now) if observed else 0
        if observed and observed > now:
            boundaries.append(observed)
        if expiry and expiry > now:
            boundaries.append(expiry)
        if observed and observed + timedelta(days=730) > now:
            boundaries.append(observed + timedelta(days=730))
        active = bool(eligible and observed and weight > 0 and (not expiry or expiry > now))
        audit.append(
            {
                "input_key": key,
                "logical_key": logical_key or key,
                "source_url": source,
                "observed_at": observed.isoformat() if observed else str(stamp) if stamp else None,
                "role": "mission_binding"
                if fact
                else "fit_geography_role"
                if eligible
                else "coverage",
                "fact_key": fact["fact_key"] if fact else None,
                "root": root or source,
                "disposition": "eligible" if active else "historical_invalid_or_coverage",
                "value_hash": hashlib.sha256(
                    (json.dumps(fact, sort_keys=True) if fact else text).encode()
                ).hexdigest(),
            }
        )
        if active and (text.strip() or fact):
            records.append(
                {
                    "id": key,
                    "logical_key": logical_key or key,
                    "source_url": source,
                    "text": text,
                    "observed_at": observed.isoformat(),
                    "weight": weight * (fact.get("confidence_weight", 1) if fact else 1),
                    "root": root or source,
                    "heading": heading,
                    "company_owned": _own(source, company),
                    **(fact or {}),
                }
            )

    latest, latest_origin, public = None, None, {}
    from .bridge import Bridge

    bridge = Bridge(company, now, add)
    snapshots_query = (
        """SELECT s.id,s.observation_json FROM snapshots s
        JOIN engine_mission_members m ON m.candidate_id=s.candidate_id
        WHERE m.company_key=? ORDER BY s.observed_at,s.id""",
        (company,),
    )
    member_query = (INVENTORY_PIN_SQL, (company,))
    execute_prefetched = getattr(store.connection, "execute_prefetched", None)
    if execute_prefetched is not None:
        rows = execute_prefetched(snapshots_query, (
            member_query, *store._frontier_queries(company, store._utc_datetime(now), None),
        ))
    else:
        rows = store.connection.execute(*snapshots_query)
    from ..entity_intelligence import project_company_self_observation

    for row in rows:
        observation = SiteObservation.model_validate_json(row["observation_json"])
        canonical = (
            (
                observation.canonical_host
                or urlsplit(observation.final_url or "").hostname
                or observation.hostname
            )
            .lower()
            .removeprefix("www.")
        )
        if canonical != company:
            audit.append(
                {
                    "input_key": f"snapshot:{row['id']}",
                    "role": "identity",
                    "disposition": "wrong_company",
                }
            )
            continue
        if observation.observed_at > now:
            boundaries.append(observation.observed_at)
        else:
            latest = observation
            latest_origin = f"snapshot:{row['id']}"
            public = observation.contacts.model_dump(mode="json")
        projected = project_company_self_observation(observation)
        bridge.snapshot(observation, f"snapshot:{row['id']}")
        main = " ".join([projected.title, projected.description, projected.text_excerpt])
        add(
            f"snapshot:{row['id']}",
            observation.final_url,
            main,
            observation.observed_at.isoformat(),
            eligible=observation.live,
            heading=projected.title,
            logical_key="owned_page:" + str(observation.final_url),
        )
        for index, page in enumerate(projected.pages):
            add(
                f"snapshot:{row['id']}:page:{index}",
                page.url,
                " ".join([page.title, page.description, page.text_excerpt]),
                observation.observed_at.isoformat(),
                eligible=observation.live and _own(page.url, company),
                heading=page.title,
                logical_key="owned_page:" + page.url,
            )
    if latest is not None and latest.live:
        bridge.owned_site_activity(latest, latest_origin)
    frontier = store.frontier_intelligence(company, as_of=now, execution_limit=None)
    bridge.editorial_blog(frontier.observations)
    for atomic in frontier.observations:
        bridge.atomic(atomic)
        add(
            "atomic:" + atomic.observation_key,
            atomic.source_url,
            " ".join([atomic.summary, _text(atomic.value), _text(atomic.qualifiers)]),
            (atomic.last_observed_at or atomic.first_observed_at).isoformat(),
            eligible=(
                atomic.score_eligible
                and not atomic.discovery_only
                and atomic.epistemic_status == "observed"
                and atomic.validity_status not in {"expired", "disputed", "historical", "unknown"}
                and not atomic.predicate.startswith("contact.")
                and atomic.qualifiers.get("subject_role")
                not in {"customer", "client", "supplier", "portfolio_client"}
            ),
            expires=atomic.expires_at,
            root=atomic.causal_root_key or atomic.source_url,
        )
    for artifact in frontier.evidence_artifacts:
        add(
            "artifact:" + artifact.artifact_key,
            artifact.canonical_url,
            artifact.excerpt,
            artifact.published_at or artifact.fetched_at,
            eligible=_own(artifact.canonical_url, company),
        )
    member = store.connection.execute(*member_query).fetchone()
    if ledger_packet is None:
        ledgers, ledger_columns = _ledger_metadata(store)
        read_ledgers = _saved_ledger_packet(store.connection, ledgers, ledger_columns, company)
        ledger_names = {table for table, _ in ledgers}
    else:
        read_ledgers = nullcontext(ledger_packet)
        ledger_names = {table for table, _, _ in ledger_packet}
    bridge.saved(store, member[0] if member else None, ledger_names)
    # Account every other saved ledger by reference/hash, without treating offer-specific
    # buyer outcomes, annotations, paid ads or inherited Beings scores as generic category facts.
    with read_ledgers as ledger_packet:
        for table, role, ledger_rows in ledger_packet:
            for row in ledger_rows:
                # Future source/validation/withdrawal times invalidate without a new write.
                for key in row.keys():  # noqa: SIM118 -- sqlite3.Row iterates values, not keys.
                    if key.endswith("_at") and (stamp := _date(row[key])) and stamp > now:
                        boundaries.append(stamp)
                    if key.endswith("_json"):
                        boundaries.extend(_future_boundaries(json.loads(row[key] or "null"), now))
                audit.append({
                    "input_key": f"{table}:{row[0]}",
                    "role": role,
                    "disposition": "typed_mission_adapter"
                    if table in bridge.used
                    else "preserved_non_fit_history_or_coverage",
                    "value_hash": hashlib.sha256(repr(tuple(row)).encode()).hexdigest(),
                })
    newest = {}
    for entry in audit:
        if (
            entry.get("logical_key")
            and (observed := _date(entry.get("observed_at")))
            and observed <= now
        ):
            key = entry["logical_key"]
            newest[key] = max(newest.get(key, ""), observed.isoformat())
    for record in records:
        key = record["logical_key"]
        newest[key] = max(newest.get(key, ""), record["observed_at"])
    records = [r for r in records if r["observed_at"] == newest[r["logical_key"]]]
    for entry in audit:
        if (
            entry.get("disposition") == "eligible"
            and entry.get("logical_key")
            and entry.get("observed_at") != newest.get(entry["logical_key"])
        ):
            entry["disposition"] = "superseded"
    return records, audit, latest, public, min(boundaries)


def _assess_fit(spec, records, *, review=None):
    if spec.get("compiler") == "ai-rubric-v1":
        from .rubric import assess_rubric

        return assess_rubric(spec, records, review=review)
    criteria, hits = [], {}
    activity_ids, role_ids = set(), set()
    for criterion in spec["topics"]:
        mentions, own, role = topic_records(spec, criterion, records)
        strength = max((r["weight"] for r in role), default=0)
        hits[criterion["id"]] = strength
        activity_ids.update(r["id"] for r in own)
        role_ids.update(r["id"] for r in role)
        criteria.append(
            {
                "id": criterion["id"],
                "role": "fit",
                "state": "inferred_match"
                if role
                else "incidental_mention"
                if mentions
                else "unknown",
                "strength": strength,
                "record_ids": sorted({r["id"] for r in role}),
                "mention_record_ids": sorted({r["id"] for r in mentions}),
            }
        )
    for criterion in [*spec["geography"], *spec.get("formats", [])]:
        matched = [r for r in records if any(_matches(r["text"], t) for t in criterion["terms"])]
        strength = max((r["weight"] for r in matched), default=0)
        hits[criterion["id"]] = strength
        criteria.append(
            {
                "id": criterion["id"],
                "role": criterion["role"],
                "state": "inferred_match" if matched else "unknown",
                "strength": strength,
                "record_ids": sorted({r["id"] for r in matched}),
            }
        )
    criteria.append(
        {
            "id": "business_role",
            "role": "business_role",
            "state": "inferred_match" if role_ids else "unknown",
            "strength": max((r["weight"] for r in records if r["id"] in role_ids), default=0),
            "record_ids": sorted(role_ids),
        }
    )
    role_known = bool(role_ids)
    if review:
        for item in review["assessments"]:
            criterion = next((c for c in criteria if c["id"] == item["criterion_id"]), None)
            if criterion is None or item["state"] == "unknown":
                continue
            cited = [r for r in records if r["id"] in item["record_ids"]]
            strength = max((r["weight"] for r in cited), default=0)
            supported = item["state"] == "supported" and bool(strength)
            hits[criterion["id"]] = strength if supported else 0
            criterion.update(
                state="ai_inferred_match" if supported else "ai_contradicted",
                strength=hits[criterion["id"]],
                record_ids=item["record_ids"],
            )
            if criterion["id"] == "business_role":
                role_known = supported
                role_ids = set(item["record_ids"]) if supported else set()
    # Core category/activity requirements are conjunctive. Generic words, location,
    # optional sales formats and a broad role cannot compensate for a missing category.
    fit = min((hits[t["id"]] for t in spec["topics"]), default=0)
    geo = min((hits[t["id"]] for t in spec["geography"]), default=1)
    format_fit = max((hits[t["id"]] for t in spec.get("formats", [])), default=0)
    eligible = bool(fit and role_known)
    score = round(50 * fit + (15 * geo + 5 if eligible else 0) + 5 * format_fit)
    if spec["geography"] and not geo:
        score = min(score, 40)
    if (
        review
        and eligible
        and any(
            i["state"] == "supported" and i["criterion_id"] in {t["id"] for t in spec["topics"]}
            for i in review["assessments"]
        )
    ):
        score = min(90, score + min(spec["ai_adjustment_limit"], 20))
        if spec["geography"] and not geo:
            score = min(score, 40)
    return (score if eligible else None), {
        "criteria": criteria,
        "role_known": role_known,
        "role_record_ids": sorted(role_ids),
        "company_activity_record_ids": sorted(activity_ids),
        "required_category_supported": bool(fit),
        "ai_reviewed": review is not None,
        "probability": False,
    }


def assess(spec, records, *, review=None):
    score, audit = _assess_fit(spec, records, review=review)
    access = [r["id"] for r in records if r.get("fact_key") == ACCESS_FACT]
    if score is not None and score > 0 and access:
        cap = 8 if spec.get("setup", {}).get("ranking_focus") == "access" else 5
        delta = min(100 - score, cap)
        audit.update(access_record_ids=access, access_score_contribution=delta)
        audit["criteria"].append({
            "id": "contact_access", "role": "access", "strength": 1,
            "record_ids": access, "score_contribution": delta,
        })
        score += delta
    return score, audit


def next_score_boundary(spec, records, score, now, expires, *, review=None):
    """Expire at the first rounded-score decay change, or an earlier source boundary.

    Between known source/review boundaries all positive weights decay monotonically.
    Do not let the local maintenance interval serve as a substitute for correctness.
    """
    if score is None:
        return expires

    def score_at(stamp):
        weighted = [
            r if r.get("fact_key") == ACCESS_FACT else {
                **r,
                "weight": durable_weight(_date(r["observed_at"]), stamp)
                * r.get("confidence_weight", 1),
            }
            for r in records
        ]
        return assess(spec, weighted, review=review)[0]

    if score_at(expires) == score:
        return expires
    low, high = now, expires
    while (high - low) > timedelta(microseconds=1):
        middle = low + (high - low) / 2
        if score_at(middle) == score:
            low = middle
        else:
            high = middle
    return high


def _empty_evidence_cache(store, row, now):
    """Reuse only a complete, still-valid empty packet across the known v5/v6 change.

    The evidence loader is identical across these policies. New predicates cannot
    gain a match from an empty eligible set; regenerate the audit with the new rubric
    while retaining all original input accounting and its exact freshness deadline.
    """
    if VERSION != "mission-local-v6" or row["previous_policy"] != "mission-local-v5":
        return None
    if row["previous_score"] is not None or row["previous_input"] != row["input_version"]:
        return None
    start, end = _date(row["previous_as_of"]), _date(row["previous_valid_until"])
    if not start or not end or not start <= now < end:
        return None
    current = store.connection.execute(
        "SELECT input_version FROM engine_mission_inventory WHERE company_key=?",
        (row["company_key"],),
    ).fetchone()
    if not current or current[0] != row["input_version"]:
        return None
    packet = store.connection.execute(
        """SELECT audit_json FROM engine_mission_scores WHERE mission_id=? AND company_key=?
        AND policy_version=? AND input_version=? AND as_of=? AND valid_until=? AND score IS NULL""",
        (
            row["previous_mission"],
            row["company_key"],
            row["previous_policy"],
            row["previous_input"],
            row["previous_as_of"],
            row["previous_valid_until"],
        ),
    ).fetchone()
    if not packet:
        return None
    try:
        audit = json.loads(packet[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(audit, dict) or audit.get("ai_reviewed") is not False:
        return None
    accounting = audit.get("accounting")
    if not isinstance(accounting, list):
        return None
    if any(
        not isinstance(a, dict)
        or a.get("disposition")
        not in {"historical_invalid_or_coverage", "coverage_requires_mission_mapping"}
        or not {"input_key", "role", "value_hash"}.issubset(a)
        for a in accounting
    ):
        return None
    effects = audit.get("source_effects")
    if effects not in ({}, {"status": "unavailable"}):
        return None
    return accounting, end, effects == {"status": "unavailable"}


def _review_query(row, mission, now):
    return (
        """SELECT cache_key,payload_json,recorded_at,valid_until FROM engine_mission_reviews
        WHERE mission_id=? AND company_key=? AND revision=? AND input_version=?
        AND valid_until>? ORDER BY recorded_at DESC,cache_key DESC LIMIT 1""",
        (mission["id"], row["company_key"], mission["revision"], row["input_version"],
         now.isoformat()),
    )


def _review(store, row, mission, now):
    return store.connection.execute(*_review_query(row, mission, now)).fetchone()


def _evaluate(store, row, mission, now, *, evidence=None):
    """Identical complete evaluation for inline and read-only process workers."""
    review = _review(store, row, mission, now)
    result = {"review_pin": tuple(review) if review else None}
    if mission["spec"].get("compiler_pending") or mission["spec"].get("compiler_error"):
        return {
            **result,
            "score": None,
            "name": None,
            "audit_json": json.dumps({"score_state": "policy_unavailable"}),
            "valid_until": (now + timedelta(hours=12)).isoformat(),
        }
    try:
        payload = json.loads(review["payload_json"]) if review else None
        name = None
        cached = _empty_evidence_cache(store, row, now)
        if cached:
            accounting, expires, unavailable = cached
            records, latest = [], None
        else:
            records, accounting, latest, public, expires = (
                evidence() if evidence is not None
                else load_evidence(store, row["company_key"], now)
            )
            unavailable = latest is None or not latest.live
        score, audit = assess(mission["spec"], records, review=payload)
        if review:
            expires = min(expires, _date(review["valid_until"]))
        if unavailable:
            score = None
        elif score and mission["spec"].get("contact_terms"):
            access, expires = access_evidence(
                store, row["company_key"], public, latest.observed_at, now, expires,
                mission["spec"]["contact_terms"],
            )
            if access:
                records = records + access
                accounting = accounting + [{
                    "input_key": r["id"], "role": "mission_access", "disposition": "eligible",
                    "source_url": r["source_url"], "observed_at": r["observed_at"],
                    "value_hash": r["id"].removeprefix("mission-access:"),
                } for r in access]
                score, audit = assess(mission["spec"], records, review=payload)
        usable = not unavailable and any(
            (
                r.get("company_owned")
                and (r.get("text", "").strip() or r.get("fact_key") in ACTIVITY_ALIASES)
            )
            or r.get("fact_key") == "company.activity"
            for r in records
        )
        audit["score_state"] = (
            "unknown" if not usable else "supported_fit" if score else "no_supported_fit"
        )
        # Zero is a completed fit evaluation with no supported points. Missing/stale
        # data remains unknown; qualification gates and their audit are unchanged.
        published_score = 0 if score is None and usable else score
        expires = next_score_boundary(
            mission["spec"],
            records,
            score,
            now,
            expires,
            review=payload,
        )
        if latest is not None:
            from ..naming import concise_company_name

            candidate = store.connection.execute(
                "SELECT label_hint,hostname FROM candidates WHERE id=?",
                (row["candidate_id"],),
            ).fetchone()
            name = concise_company_name(
                label_hint=candidate["label_hint"] or "" if candidate else "",
                title=latest.title,
                canonical_domain=row["company_key"],
                hostname=candidate["hostname"] if candidate else row["company_key"],
            )

        audit["accounting"] = accounting
        audit["source_effects"] = (
            {
                source: 0
                if score is None and not audit.get("excluded")
                else (score or 0)
                - (assess(mission["spec"], [
                    r for r in records
                    if r["root"] != source and source not in r.get("dependencies", ())
                ])[0] or 0)
                for source in sorted({
                    root for r in records for root in (r["root"], *r.get("dependencies", ()))
                })
            }
            if not review and not unavailable
            else {"status": "unavailable" if unavailable else "ai_dependency_replay_required"}
        )
        result.update(
            score=published_score,
            audit_json=json.dumps(audit),
            name=name,
            valid_until=expires.isoformat(),
        )
    except (ValueError, TypeError, KeyError) as exc:
        result.update(
            score=None,
            name=None,
            error=type(exc).__name__,
            audit_json=json.dumps({"status": "failed", "error": type(exc).__name__}),
            valid_until=(now + timedelta(minutes=15)).isoformat(),
        )
    return result


def _pins_current(store, row, mission, result, now):
    inventory = store.connection.execute(
        "SELECT candidate_id,input_version FROM engine_mission_inventory WHERE company_key=?",
        (row["company_key"],),
    ).fetchone()
    if not inventory or tuple(inventory) != (row["candidate_id"], row["input_version"]):
        return False
    try:
        current = get_mission(store.connection, mission["id"], archived=True)
    except LookupError:
        return False
    if (
        not current
        or current["archived"]
        or current["revision"] != mission["revision"]
        or current["spec"] != mission["spec"]
    ):
        return False
    review = _review(store, row, mission, now)
    if (tuple(review) if review else None) != result["review_pin"]:
        return False
    prior = store.connection.execute(
        """SELECT mission_id,revision,policy_version,score,input_version,as_of,valid_until
        FROM engine_mission_scores WHERE mission_id=? AND company_key=?""",
        (mission["id"], row["company_key"]),
    ).fetchone()
    expected = tuple(
        row[k]
        for k in (
            "previous_mission",
            "previous_revision",
            "previous_policy",
            "previous_score",
            "previous_input",
            "previous_as_of",
            "previous_valid_until",
        )
    )
    return (tuple(prior) if prior else (None,) * 7) == expected


def _publish(store, row, mission, result, now, publication_time, *, prepared_audit=False):
    """Caller owns the sole short write transaction; stale computations are discarded."""
    if (
        result.get("stale")
        or _date(result["valid_until"]) <= publication_time
        or not _pins_current(store, row, mission, result, publication_time)
    ):
        return False
    if result["name"] is not None:
        store.connection.execute(
            "UPDATE engine_mission_inventory SET company_name=? WHERE company_key=?",
            (result["name"], row["company_key"]),
        )
    store.connection.execute(
        """INSERT INTO engine_mission_scores VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mission_id,company_key) DO UPDATE SET
        candidate_id=excluded.candidate_id,revision=excluded.revision,
        input_version=excluded.input_version,policy_version=excluded.policy_version,
        score=excluded.score,as_of=excluded.as_of,valid_until=excluded.valid_until,
        audit_json=excluded.audit_json""",
        (
            mission["id"],
            row["company_key"],
            row["candidate_id"],
            mission["revision"],
            row["input_version"],
            VERSION,
            result["score"],
            now.isoformat(),
            result["valid_until"],
            result["audit_json"] if prepared_audit else encode_text(result["audit_json"], "audit"),
        ),
    )
    return True


def _select_rows(
    store, mission, limit, now, company_key, *, company_keys=None, force=False, window=None,
):
    scope, args = [], []
    if company_key is not None:
        scope.append("i.company_key=?")
        args.append(company_key)
    if company_keys is not None:
        scope.append("i.company_key IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(company_keys))
    if window is not None:
        scope.append("i.company_key>? AND i.company_key<=?")
        args.extend(window)
    order = "i.company_key" if window is not None else "s.as_of,i.company_key"
    rows = store.connection.execute(
        """SELECT i.*,s.mission_id AS previous_mission,
        s.revision AS previous_revision,
        s.policy_version AS previous_policy,s.score AS previous_score,
        s.input_version AS previous_input,s.as_of AS previous_as_of,
        s.valid_until AS previous_valid_until
        FROM engine_mission_inventory i
        LEFT JOIN engine_mission_scores s INDEXED BY idx_mission_scores_refresh
        ON s.company_key=i.company_key AND s.mission_id=?
        WHERE (? OR s.company_key IS NULL OR s.input_version<>i.input_version OR s.revision<>?
            OR s.policy_version<>? OR s.valid_until<=?)"""
        + (" AND " + " AND ".join(scope) if scope else "")
        + f" ORDER BY {order} LIMIT ?",
        (
            mission["id"], int(force), mission["revision"], VERSION, now.isoformat(), *args,
            max(1, min(int(limit), LARGE_BACKGROUND_BATCH_SIZE)),
        ),
    )
    return [dict(row) for row in rows]


def _mission_rows(store, missions, limit, now, company_key, company_keys, force, pool, on_stage):
    for mission_row in missions:
        if not getattr(pool, "accepting", True):
            break
        try:
            mission = get_mission(store.connection, mission_row["id"])
        except LookupError:
            continue
        if mission["spec"].get("compiler_pending") or mission["spec"].get("compiler_error"):
            continue
        if on_stage is not None:
            on_stage("selecting", mission["id"])
        yield mission, _select_rows(
            store, mission, limit, now, company_key, company_keys=company_keys, force=force,
        )


def publish_company_results(store, identity, packets, now, *, publication_time=None):
    """Publish prepared company packets in one transaction after rechecking every pin."""
    repo = SalesEngineRepository(store.connection)
    completed, discarded, errors = 0, 0, []
    with repo._transaction():
        same_database = store._database_identity() == identity
        publication_time = publication_time if publication_time is not None else datetime.now(UTC)
        for jobs, results in packets:
            for (mission, row), result in zip(jobs, results, strict=True):
                if not same_database or not _publish(
                    store, row, mission, result, now, publication_time,
                ):
                    discarded += 1
                elif "error" in result:
                    errors.append({"company_key": row["company_key"], "error": result["error"]})
                else:
                    completed += 1
    return completed, discarded, errors


def _refresh_company_batches(store, mission_rows, pool, now, fixed_time, on_stage):
    """Reuse evidence only for exact due jobs sharing a company and input pin."""
    identity = store._database_identity()
    completed, selected, discarded, errors = 0, 0, 0, []
    while pool.accepting and (window := list(islice(mission_rows, MISSIONS_PER_COMPANY_BATCH))):
        companies = {}
        # Admit the first due row from each mission before moving on to its next row.
        for turn in zip_longest(*(zip(repeat(mission), rows) for mission, rows in window)):
            for job in turn:
                if job is None:
                    continue
                _, row = job
                key = row["company_key"], row["candidate_id"], row["input_version"]
                companies.setdefault(key, []).append(job)
                selected += 1
        if not companies:
            continue
        if on_stage is not None:
            active = [mission["id"] for mission, rows in window if rows]
            if len(active) == 1:
                on_stage("scoring", active[0])
            else:
                on_stage("scoring_missions")
        for jobs, results in pool.evaluate_companies(store.path, identity, companies.values(), now):
            refreshed, rejected, failures = publish_company_results(
                store, identity, [(jobs, results)], now,
                publication_time=now if fixed_time else None,
            )
            completed += refreshed
            discarded += rejected
            errors.extend(failures)
    return {
        "refreshed": completed, "selected": selected, "discarded": discarded,
        "errors": errors, "provider_requests": 0,
    }


def refresh(store, *, limit=100, now=None, mission_id=None, company_key=None, compute_pool=None,
            company_keys=None, force=False, on_stage=None, coalesce=False):
    from ..desktop_ranking.control import MAX_COMPANIES, control_for, submit

    if (control := control_for(store)) is not None:
        if mission_id is None:
            raise ValueError("Specify a mission for desktop re-scoring")
        totals = {"refreshed": 0, "selected": 0, "discarded": 0, "errors": [],
                  "provider_requests": 0}
        keys = list(dict.fromkeys(company_keys)) if company_keys is not None else None
        maximum = max(1, min(int(limit), LARGE_BACKGROUND_BATCH_SIZE))
        groups = ([None] if keys is None else
                  (keys[start:start + MAX_COMPANIES]
                   for start in range(0, min(maximum, len(keys)), MAX_COMPANIES)))
        for group in groups:
            result = submit(
                store, control, "mission", mission_id=mission_id, company_key=company_key,
                company_keys=group, limit=min(MAX_COMPANIES, maximum - totals["selected"]),
                force=force, now=now.isoformat() if now is not None else None,
            )
            for name in ("refreshed", "selected", "discarded"):
                totals[name] += result[name]
            totals["errors"].extend(result["errors"])
        return totals

    fixed_time = now is not None
    now = now or datetime.now(UTC)
    if not now.tzinfo:
        raise ValueError("refresh time requires a timezone")
    now = now.astimezone(UTC)
    repo = SalesEngineRepository(store.connection)
    missions = store.connection.execute(
        "SELECT id FROM engine_missions WHERE archived=0 AND id<>'beings' AND (? IS NULL OR id=?)",
        (mission_id, mission_id),
    ).fetchall()
    completed, selected, discarded, errors = 0, 0, 0, []
    # Caller transactions may contain uncommitted evidence; never offload those reads.
    parallel = compute_pool is not None and not store.connection.in_transaction
    identity = store._database_identity() if parallel else None
    if not store.connection.in_transaction:
        _ledger_tables(store)
    mission_rows = _mission_rows(
        store, missions, limit, now, company_key, company_keys, force, compute_pool, on_stage,
    )
    if coalesce and parallel:
        return _refresh_company_batches(
            store, mission_rows, compute_pool, now, fixed_time, on_stage,
        )
    for mission, rows in mission_rows:
        selected += len(rows)
        if rows and on_stage is not None:
            on_stage("scoring", mission["id"])
        tracked = deque(r for r in rows if parallel and r["candidate_id"] and r["snapshot_id"])
        inline = [r for r in rows if not (parallel and r["candidate_id"] and r["snapshot_id"])]

        def inline_work(row, mission=mission):
            nonlocal completed, discarded
            with repo._transaction():
                started = time.monotonic()
                result = _evaluate(store, row, mission, now)
                duration = time.monotonic() - started
                published = _publish(
                    store, row, mission, result, now, now if fixed_time else datetime.now(UTC)
                )
            if not published:
                discarded += 1
            elif "error" in result:
                errors.append({"company_key": row["company_key"], "error": result["error"]})
            else:
                completed += 1
            return duration

        for row in inline:
            if not getattr(compute_pool, "accepting", True):
                break
            inline_work(row)
        durations = []
        adaptive = getattr(compute_pool, "should_parallelize", None)
        while (
            tracked
            and adaptive
            and not adaptive(durations)
            and getattr(compute_pool, "accepting", True)
        ):
            durations.append(inline_work(tracked.popleft()))
            del durations[:-4]
        if tracked:
            for row, result in compute_pool.evaluate(store.path, identity, mission, tracked, now):
                with repo._transaction():
                    published = store._database_identity() == identity and _publish(
                        store, row, mission, result, now, now if fixed_time else datetime.now(UTC)
                    )
                if not published:
                    discarded += 1
                elif "error" in result:
                    errors.append({"company_key": row["company_key"], "error": result["error"]})
                else:
                    completed += 1
    return {
        "refreshed": completed,
        "selected": selected,
        "discarded": discarded,
        "errors": errors,
        "provider_requests": 0,
    }
