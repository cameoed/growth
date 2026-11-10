"""Offline, frozen-evidence ranking evaluation; never collects or writes the source DB.

Relevance labels are separate evidence reviews, never generated from a score. Missing labels
stay in every denominator. Restored SQLite databases are disposable replay inputs only.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import socket
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ..value_utils import canonical_json_hash as _hash

SCHEMA_VERSION = "ranking-evaluation-v1"
KNOWN_CASES = ("customer.io", "smartly.io", "tecovas.com")
ROW_TABLES = (
    "buyer_evidence",
    "candidates",
    "candidate_source_edges",
    "snapshots",
    "snapshot_target_intelligence",
    "snapshot_signal_events",
    "prospect_workflow",
    "prospect_activities",
    "sales_accounts",
    "intelligence_exposures",
    "engine_buyer_facts",
    "engine_opportunities",
    "engine_research_reviews",
    "engine_workload_inputs",
    "engine_creative_annotations",
    "engine_concept_labels",
    "engine_relationships",
    "company_relationships",
    "company_email_lookups",
    "engine_decisions",
    "engine_account_states",
    "engine_watchlist",
    "scanner_executions",
    "scanner_schedule_decisions",
    "intelligence_feature_snapshots",
    "company_scan_work",
    "scanner_work",
    "candidate_ad_observations",
    "advertiser_domain_links",
    "account_claims",
)


def identity_split(company_key):
    """Aliases receive the same split; callers may supply a reviewed parent identity group."""
    return (
        "held_out"
        if int(hashlib.sha256(company_key.encode()).hexdigest()[:8], 16) % 4 == 0
        else "development"
    )


def _reference_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Evaluation reference time must include a timezone")
    return parsed.astimezone(UTC)


@contextmanager
def readonly_source(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    from ..storage_json import configure

    configure(connection)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")  # all exported rows come from one consistent read snapshot
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def freeze_cohort(source, destination, *, reference_time, leaders=120, comparison=60):
    """Export explicit evidence tables, excluding credentials/config/provider request bodies.

    Leaders plus systematic lower-ranked and segment samples are engineering comparison cases,
    not a random inventory sample. All saved website history is retained for selected identities.
    """
    reference_time = _reference_time(reference_time).isoformat()
    with readonly_source(source) as connection:
        universe = [
            dict(r)
            for r in connection.execute("""
            SELECT t.candidate_id,t.canonical_domain,t.archetype,c.hostname
            FROM snapshot_target_intelligence t
            JOIN candidates c ON c.id=t.candidate_id
            WHERE t.snapshot_id=(SELECT MAX(s.id) FROM snapshots s WHERE s.candidate_id=c.id)
            ORDER BY t.founder_priority_score DESC,t.candidate_id
        """)
        ]
        selected = universe[:leaders]
        remainder = universe[leaders:]
        if remainder and comparison:
            selected += [
                remainder[min(len(remainder) - 1, i * len(remainder) // comparison)]
                for i in range(comparison)
            ]
        selected += [row for row in universe if row["canonical_domain"] in KNOWN_CASES]
        for archetype in sorted({row["archetype"] for row in universe}):
            selected += [r for r in universe if r["archetype"] == archetype][:5]
        selected = list({row["candidate_id"]: row for row in selected}.values())
        ids = sorted(row["candidate_id"] for row in selected)
        companies = sorted({row["canonical_domain"] for row in selected})
        # Include saved aliases in the same frozen identity group and split.
        alias_rows = [row for row in universe if row["canonical_domain"] in companies]
        ids = sorted(set(ids) | {row["candidate_id"] for row in alias_rows})
        marks = ",".join("?" for _ in ids)
        cmarks = ",".join("?" for _ in companies)
        schemas = [
            dict(r)
            for r in connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND type IN ('table','index') AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name"
            )
        ]
        # Virtual tables recreate their own shadow tables. Exporting both creates duplicate
        # schemas on restore; source/cohort data remain governed by ROW_TABLES below.
        shadows = {row[1] for row in connection.execute("PRAGMA table_list") if row[2] == "shadow"}
        schemas = [row for row in schemas if row["name"] not in shadows]
        tables = {r["name"] for r in schemas if r["type"] == "table"}
        rows = {}
        for table in ROW_TABLES:
            if table not in tables:
                continue
            columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            conditions, args = [], []
            for field in ("candidate_id", "claimed_candidate_id", "completed_candidate_id"):
                if field in columns:
                    conditions.append(f'"{field}" IN ({marks})')
                    args += ids
            for field in (
                "company_key",
                "subject_company_key",
                "object_company_key",
                "subject_key",
                "object_key",
                "website_host",
            ):
                if field in columns:
                    conditions.append(f'"{field}" IN ({cmarks})')
                    args += companies
            if table == "candidates":
                conditions, args = [f"id IN ({marks})"], ids
            if table == "account_claims":
                account_ids = [r["id"] for r in rows.get("sales_accounts", [])]
                conditions = ["account_id IN (" + ",".join("?" for _ in account_ids) + ")"]
                args = account_ids
            if conditions:
                rows[table] = [
                    dict(r)
                    for r in connection.execute(
                        f'SELECT * FROM "{table}" WHERE ' + " OR ".join(conditions), args
                    )
                ]
        execution_ids = [r["id"] for r in rows.get("scanner_executions", [])]
        for table in (
            "atomic_observations",
            "coverage_manifests",
            "evidence_artifacts",
            "observation_lineage",
        ):
            rows[table] = [
                dict(r)
                for r in connection.execute(
                    f'SELECT * FROM "{table}" WHERE execution_id IN ('
                    + ",".join("?" for _ in execution_ids)
                    + ")",
                    execution_ids,
                )
            ]
        advertiser_ids = sorted(
            {r["advertiser_id"] for r in rows.get("advertiser_domain_links", [])}
        )
        for table, column, values in (
            ("advertiser_entities", "id", advertiser_ids),
            ("ad_creatives", "advertiser_id", advertiser_ids),
            (
                "ad_discovery_runs",
                "id",
                sorted({r["run_id"] for r in rows.get("candidate_ad_observations", [])}),
            ),
        ):
            rows[table] = [
                dict(r)
                for r in connection.execute(
                    f'SELECT * FROM "{table}" WHERE "{column}" IN ('
                    + ",".join("?" for _ in values)
                    + ")",
                    values,
                )
            ]
        for row in rows["ad_discovery_runs"]:
            # These fields are unnecessary for deterministic ad-ledger hydration.
            row["request_json"] = "{}"
            row["estimate_json"] = "{}"
        outcome_rows = [
            dict(r)
            for r in connection.execute(
                "SELECT id,website_host,lifecycle,outcome_json,delivery_json,discovery_json "
                "FROM sales_accounts"
            )
        ]
        cohort = [
            {
                "candidate_id": row["candidate_id"],
                "company_key": row["canonical_domain"],
                "identity_group": row["canonical_domain"],
                "split": identity_split(row["canonical_domain"]),
                "selection": "known_regression"
                if row["canonical_domain"] in KNOWN_CASES
                else "leader"
                if row in universe[:leaders]
                else "broader_comparison",
            }
            for row in alias_rows
        ]
        package = {
            "schema_version": SCHEMA_VERSION,
            "reference_time": reference_time,
            "cohort": cohort,
            "inventory_inspected_rows": len(universe),
            "selection_policy": {
                "leaders": leaders,
                "systematic_comparison": comparison,
                "segment_examples": 5,
                "known_cases": KNOWN_CASES,
            },
            "schemas": schemas,
            "tables": rows,
            "existing_outcomes": outcome_rows,
            "labels": {},
            "provider_cost_microusd": 0,
            "limitations": [
                "Purposeful saved-data cohort; not inventory-wide recall.",
                "Canonical-domain groups; corporate-parent relationships remain unreviewed.",
                "Source data are saved observations, not independently verified business truth.",
            ],
        }
        package["evidence_sha256"] = _hash(rows)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wt") as stream:
        json.dump(package, stream, separators=(",", ":"))
    return {
        "companies": len(companies),
        "candidate_rows": len(ids),
        "table_rows": {k: len(v) for k, v in rows.items()},
        "evidence_sha256": package["evidence_sha256"],
        "bytes": destination.stat().st_size,
    }


def load_package(path):
    with gzip.open(path, "rt") as stream:
        package = json.load(stream)
    if package["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported evaluation package")
    if _hash(package["tables"]) != package["evidence_sha256"]:
        raise ValueError("Frozen evidence checksum mismatch")
    return package


def restore_package(package, path):
    """Materialize only to a new disposable DB; never overwrite an existing database."""
    if Path(path).exists():
        raise FileExistsError(path)
    connection = sqlite3.connect(path)
    try:
        for schema in package["schemas"]:
            if schema["type"] == "table":
                connection.execute(schema["sql"])
        for table, rows in package["tables"].items():
            if not rows:
                continue
            columns = list(rows[0])
            names = ",".join(f'"{name}"' for name in columns)
            marks = ",".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                [[r[c] for c in columns] for r in rows],
            )
        for schema in package["schemas"]:
            if schema["type"] == "index":
                connection.execute(schema["sql"])
        connection.commit()
    finally:
        connection.close()


def reviewed_metrics(ranked, labels, *, limit):
    selected = ranked[:limit]
    denominator = len(selected)
    counts = Counter(
        labels.get(row["company_key"], {}).get("relevance", "unreviewed") for row in selected
    )
    return {
        "requested": limit,
        "denominator": denominator,
        **{key: counts[key] for key in ("strong", "possible", "weak", "uncertain", "unreviewed")},
        "strong_relevance_share": counts["strong"] / denominator if denominator else None,
        "reviewed_share": (denominator - counts["unreviewed"]) / denominator
        if denominator
        else None,
    }


def validate_labels(labels):
    """Reject anonymous or score-derived labels instead of quietly calling them reviewed."""
    for company, label in labels.items():
        if label.get("relevance") not in {"strong", "possible", "weak", "uncertain"}:
            raise ValueError(f"Invalid relevance label for {company}")
        if not all(
            label.get(key)
            for key in ("reviewer", "source_urls", "source_observed_at", "rationale", "uncertainty")
        ):
            raise ValueError(f"Review provenance missing for {company}")


def outcome_availability(package, now):
    from .learning import _facts, _metrics

    facts = [
        _facts({**row, "company_key": row["website_host"]}, now)
        for row in package.get("existing_outcomes", [])
    ]
    return {
        "recorded": _metrics(facts),
        "score_band_analysis": None,
        "reason": "No historical selection-score outcome linkage in the frozen records.",
        "absence_of_outcome_is_loss": False,
    }


def _known_regressions(store, rows, now):
    from ..models import AdIntelligence
    from ..scoring import score_candidate

    result = {}
    for row in rows:
        if row["company_key"] not in KNOWN_CASES:
            continue
        _, candidate, observation, _ = store.current_candidate_score(
            row["candidate_id"], reference_time=now
        )
        identical = score_candidate(
            candidate,
            observation.model_copy(deep=True),
            previous_observation=observation.model_copy(deep=True),
            reference_time=now,
            ad_intelligence=AdIntelligence.model_validate(
                store.current_candidate_ad_intelligence(row["candidate_id"])
            ),
        )
        increases = [
            event.model_dump(mode="json")
            for event in identical.creative_capacity.signal_events
            if event.event_type == "creative_hiring_increase_since_prior_observation"
        ]
        result[row["company_key"]] = {
            "identical_observation_hiring_increases": increases,
            "passed": not increases,
            "reference": "docs/AUDITS/data-to-sales-insights-2026-09-17.md",
        }
    return result


def _score_target(store, candidate, observation, score, now):
    target = score.creative_capacity.model_dump(mode="json")
    lane, scope = store._target_lane_and_scope(candidate, score)
    return {
        **target,
        "canonical_domain": observation.canonical_host,
        "company_role": score.company_role.model_dump(mode="json"),
        "priority_score": score.creative_capacity.opportunity.founder_priority,
        "target_scope": scope,
        "relationship_lane": lane,
        "disqualified": score.disqualified,
        "computed_at": now.isoformat(),
    }


def _perturbations(store, candidate_id, now, assessment, score):
    """Remove causal event families at their shared aggregation boundary, then fully rescore.

    Legacy non-event feature priors remain fixed. Effects are model sensitivity, not causal lift.
    """
    from .. import revenue_intelligence as revenue
    from .priority import build_sales_priority

    original = revenue.cluster_signal_events
    families = sorted({e.family for e in score.creative_capacity.signal_events})
    results = {}
    for family in families:

        def remove(events, *, now=None, removed=family):
            return original([e for e in events if e.family != removed], now=now)

        with patch.object(revenue, "cluster_signal_events", remove):
            current = store.current_candidate_score(candidate_id, reference_time=now)
        _, candidate, observation, changed = current
        target = _score_target(store, candidate, observation, changed, now)
        priority = build_sales_priority(target, assessment, now=now)
        results[family] = {
            "score": priority["score"],
            "dimensions": changed.creative_capacity.opportunity.model_dump(mode="json"),
        }

    def duplicates(events, *, now=None):
        return original([*events, *events, *events], now=now)

    with patch.object(revenue, "cluster_signal_events", duplicates):
        _, candidate, observation, duplicate = store.current_candidate_score(
            candidate_id, reference_time=now
        )
    duplicate_target = _score_target(store, candidate, observation, duplicate, now)
    later = now + timedelta(days=365)
    _, candidate, observation, expired = store.current_candidate_score(
        candidate_id, reference_time=later
    )
    expired_target = _score_target(store, candidate, observation, expired, later)
    return {
        "family_removal": results,
        "duplicate_score": build_sales_priority(duplicate_target, assessment, now=now)["score"],
        "after_365_days_score": build_sales_priority(expired_target, assessment, now=later)[
            "score"
        ],
        "after_365_days_live_timing_events": sum(
            e.family == "commercial_timing" and e.current_strength > 0
            for e in expired.creative_capacity.signal_events
        ),
    }


def evaluate_package(package, *, labels=None, perturb_limit=20):
    from .. import revenue_intelligence
    from ..store import LeadStore
    from . import priority, service
    from .buyer import build_buyer_assessment
    from .contacts import recommend_contacts
    from .hydrated_scoring import VERSION as hydration_version
    from .priority import build_sales_priority

    now = _reference_time(package["reference_time"])
    labels = labels if labels is not None else package.get("labels", {})
    validate_labels(labels)
    projected = importlib.util.find_spec("beings_leads.sales_engine.ranking") is not None
    start = time.perf_counter()
    ranked, errors = [], []
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append("blocked")
        raise RuntimeError("Ranking evaluation forbids network access")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    with (
        TemporaryDirectory(prefix="beings-ranking-eval-") as temporary,
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket, "create_connection", deny_network),
    ):
        path = Path(temporary) / "cohort.db"
        restore_package(package, path)
        with LeadStore(path, initialize=False) as store:
            engine = service.SalesEngineService(store)
            # Disable score caches: interventions on the same evidence must be independently run.
            store.connection.execute("BEGIN")
            for case in package["cohort"]:
                try:
                    current = store.current_candidate_score(
                        case["candidate_id"], reference_time=now
                    )
                    if current is None:
                        continue
                    source_row, candidate, observation, score = current
                    key = observation.canonical_host or candidate.hostname
                    target = _score_target(store, candidate, observation, score, now)
                    context = engine._buyer_context(case["candidate_id"], key)
                    context.update(
                        role=target["company_role"], target=target, suggested_opportunities=[]
                    )
                    assessment = build_buyer_assessment(
                        context, engine.buyer_facts.list(key), now=now
                    )
                    result = build_sales_priority(target, assessment, now=now)
                    suppressed = store.connection.execute(
                        "SELECT target_is_suppressed(?,?,?)",
                        (candidate.source_apex, candidate.hostname, key),
                    ).fetchone()[0]
                    excluded = any(
                        item.get("excluded")
                        and item["candidate_id"]
                        in {
                            case["candidate_id"]
                            for case in package["cohort"]
                            if case["company_key"] == key
                        }
                        for item in package["tables"].get("prospect_workflow", [])
                    )
                    contacts = recommend_contacts(
                        {
                            "company_key": key,
                            "public_contacts": observation.contacts.model_dump(mode="json"),
                            "licensed_professional_contacts": store.treg_people_intelligence(
                                key, as_of=now
                            ),
                            "observed_at": observation.observed_at,
                            "role": target["company_role"],
                            "as_of": now,
                        }
                    )
                    events = score.creative_capacity.signal_events
                    ranked.append(
                        {
                            **case,
                            "company_key": key,
                            "snapshot_id": source_row["snapshot_id"],
                            "score": result["score"],
                            "policy": result["version"],
                            "role": score.company_role.role,
                            "archetype": score.archetype,
                            "relationship_lane": target["relationship_lane"],
                            "eligible": not score.disqualified
                            and not suppressed
                            and not excluded
                            and observation.live
                            and target["target_scope"] != "out_of_scope"
                            and (not projected or target["relationship_lane"] != "referral_partner")
                            and assessment["readiness"] not in {"deprioritize", "nurture"}
                            and score.company_role.role != "directory_or_awards",
                            "buyer_readiness": assessment["readiness"],
                            "contributions": result["factors"],
                            "dimensions": score.creative_capacity.opportunity.model_dump(
                                mode="json"
                            ),
                            "families": [
                                c.model_dump(mode="json")
                                for c in score.creative_capacity.signal_clusters
                            ],
                            "evidence": [e.model_dump(mode="json") for e in events],
                            "contacts": contacts,
                            "snapshot_observed_at": observation.observed_at.isoformat(),
                            "assessment": assessment,
                        }
                    )
                except Exception as error:
                    errors.append({"candidate_id": case["candidate_id"], "error": repr(error)})
            # Canonical identities count once in ranking and every metric.
            by_identity = {}
            for row in sorted(ranked, key=lambda r: (-r["snapshot_id"], r["candidate_id"])):
                by_identity.setdefault(row["company_key"], row)
            ranked = sorted(
                by_identity.values(), key=lambda r: (-(r["score"] or 0), r["company_key"])
            )
            regressions = _known_regressions(store, ranked, now)
            for row in ranked[:perturb_limit]:
                try:
                    current = store.current_candidate_score(row["candidate_id"], reference_time=now)
                    row["perturbations"] = _perturbations(
                        store, row["candidate_id"], now, row["assessment"], current[-1]
                    )
                except Exception as error:
                    errors.append(
                        {"candidate_id": row["candidate_id"], "perturbation_error": repr(error)}
                    )
            store.connection.rollback()
            projection = {"available": projected}
            if projected:
                from .ranking import refresh_rankings

                projection_start = time.perf_counter()
                batches = []
                for _ in range(100):
                    batch = refresh_rankings(store, limit=250, now=now)
                    batches.append(batch)
                    if batch["backfill_complete"] and batch["selected"] == 0:
                        break
                projection.update(
                    batches=batches,
                    runtime_seconds=round(time.perf_counter() - projection_start, 3),
                )
            list_start = time.perf_counter()
            try:
                with patch.object(service, "datetime", FrozenDateTime):
                    queue = engine.queue(
                        limit=50,
                        prospects_only=False,
                        **({"reference_time": now} if projected else {}),
                    )
                actual = [r["company_key"] for r in queue["items"]]
                queue_result = {
                    "company_keys": actual,
                    "coverage": queue.get("coverage", {}),
                    "runtime_ms": round((time.perf_counter() - list_start) * 1000, 2),
                }
            except Exception as error:
                actual = []
                queue_result = {"error": repr(error)}
    eligible = [r for r in ranked if r["eligible"]]
    strongest = [
        r["company_key"]
        for r in ranked
        if labels.get(r["company_key"], {}).get("relevance") == "strong"
    ]
    quality = {}
    for limit in (20, 50):
        selected = eligible[:limit]
        denominator = len(selected)
        quality[str(limit)] = {
            **reviewed_metrics(eligible, labels, limit=limit),
            "relevant_named_contact_count": sum(
                bool(r["contacts"].get("people")) for r in selected
            ),
            "referral_only_count": sum(
                r["relationship_lane"] == "referral_partner" for r in selected
            ),
            "wrong_identity_reviewed_count": sum(
                labels.get(r["company_key"], {}).get("wrong_identity") is True for r in selected
            ),
            "unsupported_demand_reviewed_count": sum(
                labels.get(r["company_key"], {}).get("unsupported_demand") is True for r in selected
            ),
            "unsupported_demand_unknown_count": sum(
                "unsupported_demand" not in labels.get(r["company_key"], {}) for r in selected
            ),
            "identity_accuracy_unknown_count": sum(
                "wrong_identity" not in labels.get(r["company_key"], {}) for r in selected
            ),
            "contact_accuracy_unknown_count": denominator,
            "expired_trigger_count": sum(
                any(
                    e["family"] == "commercial_timing"
                    and e["expires_at"]
                    and datetime.fromisoformat(e["expires_at"]) <= now
                    and e["current_strength"] > 0
                    for e in r["evidence"]
                )
                for r in selected
            ),
        }
    for row in ranked:
        row.pop("assessment", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "reference_time": package["reference_time"],
        "evidence_sha256": package["evidence_sha256"],
        "policy_version": priority.VERSION,
        "revenue_policy_version": getattr(revenue_intelligence, "VERSION", "unversioned"),
        "hydration_version": hydration_version,
        "ranked": ranked,
        "top_metrics": quality,
        "split_metrics": {
            split: reviewed_metrics([r for r in eligible if r["split"] == split], labels, limit=50)
            for split in ("development", "held_out")
        },
        "segment_metrics": {
            segment: reviewed_metrics(
                [r for r in eligible if r["archetype"] == segment], labels, limit=50
            )
            for segment in sorted({r["archetype"] for r in eligible})
        },
        "actual_query": queue_result,
        "projection": projection,
        "retrieval": {
            "broader_top50": len(eligible[:50]),
            "broader_top50_recovered": len(set(actual) & {r["company_key"] for r in eligible[:50]}),
            "reviewed_strong_denominator": len(strongest),
            "reviewed_strong_recovered": len(set(actual) & set(strongest)),
            "duplicate_leakage": len(actual) - len(set(actual)),
        },
        "runtime_seconds": round(time.perf_counter() - start, 3),
        "provider_cost_microusd": 0,
        "network_calls": 0,
        "blocked_network_attempts": len(network_attempts),
        "known_regressions": regressions,
        "existing_outcome_accounts": len(package.get("existing_outcomes", [])),
        "outcomes": outcome_availability(package, now),
        "review_count": len(labels),
        "human_review_count": sum(value.get("human_reviewed") is True for value in labels.values()),
        "review_effort_seconds": (
            sum(value["review_seconds"] for value in labels.values())
            if labels
            and all(
                isinstance(value.get("review_seconds"), (int, float)) for value in labels.values()
            )
            else None
        ),
        "errors": errors,
        "limitations": [
            *package["limitations"],
            "Family removal holds non-event legacy feature priors fixed; "
            "measures model sensitivity only.",
            "Duplicate intervention copies causal events, not the original media pages.",
            "Employment accuracy is unreviewed; source-supported contact coverage "
            "is not deliverability.",
            "Actual-query recovery is within the frozen comparison DB, not inventory-wide recall.",
        ],
    }


def compare_runs(before, after):
    if (
        before["evidence_sha256"] != after["evidence_sha256"]
        or before["reference_time"] != after["reference_time"]
    ):
        raise ValueError("Before/after require identical frozen evidence and reference time")
    old = {row["company_key"]: (index + 1, row) for index, row in enumerate(before["ranked"])}
    changes = [
        {
            "company_key": row["company_key"],
            "split": row["split"],
            "archetype": row["archetype"],
            "before_rank": old[row["company_key"]][0],
            "after_rank": index + 1,
            "before_score": old[row["company_key"]][1]["score"],
            "after_score": row["score"],
        }
        for index, row in enumerate(after["ranked"])
        if row["company_key"] in old
    ]
    return {
        "before_policy": before["policy_version"],
        "after_policy": after["policy_version"],
        "before_hydration_policy": before["hydration_version"],
        "after_hydration_policy": after["hydration_version"],
        "before_revenue_policy": before.get("revenue_policy_version", "unversioned"),
        "after_revenue_policy": after.get("revenue_policy_version", "unversioned"),
        "evidence_sha256": after["evidence_sha256"],
        "reference_time": after["reference_time"],
        "changes": changes,
        "top_metrics": {"before": before["top_metrics"], "after": after["top_metrics"]},
        "retrieval": {"before": before["retrieval"], "after": after["retrieval"]},
        "errors": {"before": before["errors"], "after": after["errors"]},
    }
