"""Canonical, versioned Sales rankings from bounded local background work.

The list reads this projection, never a preselected cohort scored during the request.
Source writes invalidate it in the same transaction via SQLite triggers. No provider
is called, and neither schema installation nor a list request backfills the inventory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from ..treg_people import SCANNER_ID as PEOPLE_SCANNER_ID
from .persistence import SalesEngineRepository

VERSION = "company-ranking-v2-receipt-retry-boundaries"
MAX_AGE = timedelta(hours=12)


def policy_version() -> str:
    from .. import revenue_intelligence
    from ..evidence_policy import VERSION as FRESHNESS_VERSION
    from . import hydrated_scoring, priority
    from .evidence_accounting import VERSION as ACCOUNTING_VERSION

    return ":".join(
        (
            VERSION,
            hydrated_scoring.VERSION,
            priority.VERSION,
            getattr(revenue_intelligence, "VERSION", "revenue-v1"),
            ACCOUNTING_VERSION,
            FRESHNESS_VERSION,
        )
    )


def _time(value=None):
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ranking reference time requires a timezone")
    return value.astimezone(UTC)


SCHEMA = (
    """CREATE INDEX IF NOT EXISTS idx_snapshots_candidate_observed
        ON snapshots(candidate_id,observed_at DESC,id DESC)""",
    """CREATE TABLE IF NOT EXISTS engine_ranking_members(
        candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
        company_key TEXT NOT NULL, snapshot_id INTEGER NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_ranking_members_company
        ON engine_ranking_members(company_key,snapshot_id DESC,candidate_id)""",
    """CREATE TABLE IF NOT EXISTS engine_ranking_revisions(
        candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS engine_ranking_pending(
        candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
        enqueued_at TEXT NOT NULL, due_at TEXT NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0, error_kind TEXT NOT NULL DEFAULT '')""",
    """CREATE INDEX IF NOT EXISTS idx_engine_ranking_pending_due
        ON engine_ranking_pending(due_at,candidate_id)""",
    """CREATE TABLE IF NOT EXISTS engine_company_ranking(
        company_key TEXT PRIMARY KEY, candidate_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL, company_name TEXT NOT NULL, score INTEGER,
        eligible INTEGER NOT NULL, excluded INTEGER NOT NULL DEFAULT 0,
        policy_version TEXT NOT NULL, evidence_version TEXT NOT NULL,
        as_of TEXT NOT NULL, valid_until TEXT NOT NULL, dirty INTEGER NOT NULL DEFAULT 0,
        target_json TEXT NOT NULL, assessment_json TEXT NOT NULL, priority_json TEXT NOT NULL,
        item_json TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_company_ranking_scores
        ON engine_company_ranking(policy_version,dirty,eligible,score DESC,company_key)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_company_ranking_search
        ON engine_company_ranking(policy_version,dirty,score DESC,company_key)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_company_ranking_expiry
        ON engine_company_ranking(dirty,valid_until,candidate_id)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_company_ranking_candidate
        ON engine_company_ranking(candidate_id)""",
    """CREATE TABLE IF NOT EXISTS engine_ranking_backfill(
        policy_version TEXT PRIMARY KEY, last_candidate_id INTEGER NOT NULL DEFAULT 0,
        complete INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        live_phase INTEGER NOT NULL DEFAULT 1)""",
)

# Every scorer input has an invalidation owner. UPDATE and DELETE are intentional:
# operator corrections and negative evidence matter as much as appended positives.
INPUT_TABLES = (
    "buyer_evidence",
    "candidates",
    "snapshots",
    "snapshot_target_intelligence",
    "candidate_source_edges",
    "atomic_observations",
    "coverage_manifests",
    "evidence_artifacts",
    "intelligence_feature_snapshots",
    "scanner_executions",
    "company_email_lookups",
    "candidate_ad_observations",
    "ad_discovery_runs",
    "ad_creatives",
    "advertiser_entities",
    "advertiser_domain_links",
    "market_events",
    "market_entity_mentions",
    "market_documents",
    "company_relationships",
    "sales_accounts",
    "account_claims",
    "sales_account_events",
    "prospect_workflow",
    "engine_buyer_facts",
    "engine_opportunities",
    "engine_research_reviews",
    "engine_relationships",
    "engine_creative_annotations",
    "engine_workload_inputs",
    "engine_concept_labels",
)


def initialize_schema(connection) -> None:
    """Install additive tables/triggers only; safe for caller-owned transactions."""
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"candidates", "snapshots", "snapshot_target_intelligence"} <= tables:
        return
    # Standalone repositories and legacy test fixtures may have partial parent tables.
    if "hostname" not in {r[1] for r in connection.execute("PRAGMA table_info(candidates)")}:
        return
    for statement in SCHEMA:
        connection.execute(statement)
    from .ranking_modes import SCHEMA as LENS_SCHEMA

    for statement in LENS_SCHEMA:
        connection.execute(statement)
    from .evidence_accounting import initialize_storage

    initialize_storage(connection)
    if "live_phase" not in {
        r[1] for r in connection.execute("PRAGMA table_info(engine_ranking_backfill)")
    }:
        connection.execute(
            "ALTER TABLE engine_ranking_backfill ADD COLUMN live_phase INTEGER NOT NULL DEFAULT 1"
        )
    triggers = {
        r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    # Trigger definitions are versioned just like derived policy. Updating their
    # coverage must not leave already-initialized databases on an older write graph.
    for name in triggers:
        if name.startswith(("engine_rank_v1_", "engine_rank_v2_", "engine_rank_v3_")):
            connection.execute(f"DROP TRIGGER {name}")
    for table in INPUT_TABLES:
        if table not in tables:
            continue
        columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
        for action, refs in (
            ("INSERT", ("NEW",)),
            ("DELETE", ("OLD",)),
            ("UPDATE", ("OLD", "NEW")),
        ):
            contact_tables = {"company_email_lookups", "scanner_executions",
                              "coverage_manifests", "evidence_artifacts"}
            version = "v5" if table in contact_tables else "v4"
            name = f"engine_rank_{version}_{table}_{action.lower()}"
            if table in contact_tables:
                connection.execute(
                    f"DROP TRIGGER IF EXISTS engine_rank_v4_{table}_{action.lower()}"
                )
            if name in triggers:
                continue
            selections = []
            for ref in refs:
                if table == "candidates":
                    selections.append(f"SELECT {ref}.id AS candidate_id")
                if "candidate_id" in columns:
                    selections.append(f"SELECT {ref}.candidate_id AS candidate_id")
                if "direct_candidate_id" in columns:
                    selections.append(f"SELECT {ref}.direct_candidate_id AS candidate_id")
                keys = [
                    f"{ref}.{key}"
                    for key in (
                        "company_key",
                        "subject_company_key",
                        "object_company_key",
                        "canonical_domain",
                        "website_host",
                        "subject_key",
                        "object_key",
                        "direct_company_key",
                        "domain",
                        "landing_domain",
                    )
                    if key in columns
                ]
                for key in keys:
                    selections.append(
                        "SELECT candidate_id FROM engine_ranking_members "
                        f"WHERE company_key=replace({key},'domain:','')"
                    )
                    selections.append(
                        f"SELECT id FROM candidates WHERE hostname=replace({key},'domain:','')"
                    )
                if "account_id" in columns and "sales_accounts" in tables:
                    selections.append(
                        f"SELECT candidate_id FROM sales_accounts WHERE id={ref}.account_id"
                    )
                    selections.append(
                        "SELECT m.candidate_id FROM engine_ranking_members m "
                        "JOIN sales_accounts a ON a.website_host=m.company_key "
                        f"WHERE a.id={ref}.account_id"
                    )
                if table == "market_documents" and "market_events" in tables:
                    selections.append(
                        "SELECT direct_candidate_id AS candidate_id FROM market_events "
                        f"WHERE document_id={ref}.id"
                    )
                    selections.append(
                        "SELECT candidate_id FROM market_entity_mentions "
                        f"WHERE document_id={ref}.id"
                    )
                if table == "market_events":
                    selections.append(
                        f"SELECT candidate_id FROM market_entity_mentions WHERE id={ref}.mention_id"
                    )
                if table == "candidates" and action == "DELETE":
                    selections.append(
                        "SELECT m.candidate_id FROM engine_ranking_members m "
                        "JOIN engine_company_ranking r ON r.company_key=m.company_key "
                        f"WHERE r.candidate_id={ref}.id"
                    )
                if table == "ad_discovery_runs":
                    selections.append(
                        f"SELECT candidate_id FROM candidate_ad_observations WHERE run_id={ref}.id"
                    )
                if table in {"ad_creatives", "advertiser_entities"}:
                    identity = (
                        f"{ref}.id" if table == "advertiser_entities" else (f"{ref}.advertiser_id")
                    )
                    selections.append(
                        "SELECT candidate_id FROM advertiser_domain_links "
                        f"WHERE advertiser_id={identity}"
                    )
                    selections.append(
                        "SELECT m.candidate_id FROM engine_ranking_members m "
                        "JOIN advertiser_domain_links l ON l.domain=m.company_key "
                        f"WHERE l.advertiser_id={identity}"
                    )
            if not selections:
                continue
            affected = " UNION ".join(selections)
            affected = (
                f"SELECT candidate_id FROM ({affected}) UNION SELECT candidate_id "
                "FROM engine_ranking_members WHERE company_key IN (SELECT company_key "
                f"FROM engine_ranking_members WHERE candidate_id IN ({affected}))"
            )
            # Filter deleted/uninspected records; unknown companies never become zero.
            candidates = (
                f"SELECT DISTINCT a.candidate_id FROM ({affected}) a "
                "JOIN candidates c ON c.id=a.candidate_id "
                "WHERE EXISTS(SELECT 1 FROM snapshots s WHERE s.candidate_id=a.candidate_id) "
                "OR EXISTS(SELECT 1 FROM engine_ranking_members m "
                "WHERE m.candidate_id=a.candidate_id)"
            )
            timestamp = "strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"
            body = f"""
                INSERT INTO engine_ranking_revisions(candidate_id,revision)
                    SELECT candidate_id,1 FROM ({candidates}) WHERE 1
                    ON CONFLICT(candidate_id) DO UPDATE SET revision=revision+1;
                INSERT INTO engine_ranking_pending(candidate_id,enqueued_at,due_at)
                    SELECT candidate_id,{timestamp},{timestamp} FROM ({candidates}) WHERE 1
                    ON CONFLICT(candidate_id) DO UPDATE SET due_at=excluded.due_at,
                        failures=0,error_kind='';
                UPDATE engine_company_ranking SET dirty=1 WHERE company_key IN (
                    SELECT company_key FROM engine_ranking_members
                    WHERE candidate_id IN ({candidates}));
            """
            # Identity corrections invalidate both old and new groups. Snapshot projection
            # writes are ordered after raw snapshots in the existing save path.
            if table == "snapshot_target_intelligence" and action != "DELETE":
                body += """
                    INSERT INTO engine_ranking_members VALUES(
                        NEW.candidate_id,NEW.canonical_domain,NEW.snapshot_id)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        company_key=excluded.company_key,snapshot_id=excluded.snapshot_id
                    WHERE excluded.snapshot_id>=engine_ranking_members.snapshot_id;
                """
            if table == "candidates" and action == "DELETE":
                body += "DELETE FROM engine_company_ranking WHERE candidate_id=OLD.id;"
            timing = "BEFORE" if table == "candidates" and action == "DELETE" else "AFTER"
            # Contact attempts and empty provider receipts are operational facts, not
            # changes to buyer evidence. Keep a current published ranking visible.
            guard = ""
            if table == "company_email_lookups":
                guard = " OR ".join(
                    f"({ref}.email_verified=1 AND COALESCE({ref}.work_email,'')!='')"
                    for ref in refs
                )
            elif table == "scanner_executions":
                guard = " OR ".join(f"{ref}.scanner_id!='{PEOPLE_SCANNER_ID}'" for ref in refs)
            elif table in {"coverage_manifests", "evidence_artifacts"}:
                guard = " OR ".join(
                    f"({ref}.scanner_id!='{PEOPLE_SCANNER_ID}' OR "
                    f"COALESCE(json_extract({ref}.metadata_json,'$.people_selected'),-1)!=0)"
                    for ref in refs
                )
            when = f" WHEN {guard}" if guard else ""
            connection.execute(
                f"CREATE TRIGGER {name} {timing} {action} ON {table}{when} BEGIN {body} END"
            )

    from .read_indexes import initialize as initialize_read_indexes

    initialize_read_indexes(connection)


def enqueue(store, candidate_ids, *, now=None) -> int:
    stamp = _time(now).isoformat()
    ids = list(dict.fromkeys(int(i) for i in candidate_ids))
    for candidate_id in ids:
        store.connection.execute(
            "INSERT INTO engine_ranking_pending(candidate_id,enqueued_at,due_at) VALUES(?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET due_at=excluded.due_at",
            (candidate_id, stamp, stamp),
        )
    return len(ids)


def _evidence_version(connection, company_key):
    revisions = [
        tuple(row)
        for row in connection.execute(
            "SELECT m.candidate_id,m.snapshot_id,COALESCE(r.revision,0) "
            "FROM engine_ranking_members m LEFT JOIN engine_ranking_revisions r "
            "ON r.candidate_id=m.candidate_id WHERE m.company_key=? ORDER BY m.candidate_id",
            (company_key,),
        )
    ]
    return hashlib.sha256(repr(revisions).encode()).hexdigest()


def _publication_is_current(connection, company_key, now, version):
    """Reuse only a complete publication of these exact aliases and evidence revisions."""
    row = connection.execute(
        "SELECT evidence_version FROM engine_company_ranking WHERE company_key=? "
        "AND policy_version=? AND dirty=0 AND as_of<=? AND valid_until>?",
        (company_key, version, now.isoformat(), now.isoformat()),
    ).fetchone()
    return row is not None and row[0] == _evidence_version(connection, company_key)


def seed_backfill(store, *, limit=100, now=None) -> dict:
    """Resume keyset inventory traversal. Policy changes start an explicit new pass."""
    now = _time(now)
    connection = store.connection
    version = policy_version()
    connection.execute(
        "INSERT OR IGNORE INTO engine_ranking_backfill(policy_version,updated_at) VALUES(?,?)",
        (version, now.isoformat()),
    )
    state = connection.execute(
        "SELECT * FROM engine_ranking_backfill WHERE policy_version=?", (version,)
    ).fetchone()
    if state["complete"]:
        return {"seeded": 0, "backfill_complete": True}
    limit = max(1, min(int(limit), 1000))
    # Correlated primary-key lookups keep this a bounded candidate walk. A join on ti.live
    # lets SQLite build a bloom filter by scanning every hydrated row for every small batch.
    # Low-ranked inspected companies are still included; there is no score cutoff.
    rows = connection.execute(
        "SELECT c.id,(SELECT ti.canonical_domain FROM snapshot_target_intelligence ti "
        "WHERE ti.snapshot_id=s.id) AS canonical_domain,s.id AS snapshot_id FROM candidates c "
        "JOIN snapshots s ON s.id=(SELECT id FROM snapshots WHERE candidate_id=c.id "
        "ORDER BY id DESC LIMIT 1) "
        "WHERE c.id>? AND (SELECT ti.live FROM snapshot_target_intelligence ti "
        "WHERE ti.snapshot_id=s.id)=? ORDER BY c.id LIMIT ?",
        (state["last_candidate_id"], state["live_phase"], limit),
    ).fetchall()
    for row in rows:
        connection.execute(
            "INSERT INTO engine_ranking_members VALUES(?,?,?) ON CONFLICT(candidate_id) "
            "DO UPDATE SET company_key=excluded.company_key,snapshot_id=excluded.snapshot_id "
            "WHERE company_key IS NOT excluded.company_key "
            "OR snapshot_id IS NOT excluded.snapshot_id",
            (row["id"], row["canonical_domain"], row["snapshot_id"]),
        )
    # Each identity is seeded once per policy. A new policy bypasses an old policy's
    # retry clock; retries within a completed backfill remain independently scheduled.
    seeded = 0
    for row in rows:
        if _publication_is_current(connection, row["canonical_domain"], now, version):
            continue
        connection.execute(
            "INSERT INTO engine_ranking_pending(candidate_id,enqueued_at,due_at) "
            "VALUES(?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
            "due_at=excluded.due_at,failures=0,error_kind=''",
            (row["id"], now.isoformat(), now.isoformat()),
        )
        seeded += 1
    phase_done = len(rows) < limit
    complete = phase_done and not state["live_phase"]
    switch_phase = phase_done and bool(state["live_phase"])
    connection.execute(
        "UPDATE engine_ranking_backfill SET last_candidate_id=?,complete=?,updated_at=?,"
        "live_phase=? "
        "WHERE policy_version=?",
        (
            0 if switch_phase else rows[-1]["id"] if rows else state["last_candidate_id"],
            int(complete),
            now.isoformat(),
            0 if switch_phase else state["live_phase"],
            version,
        ),
    )
    return {"seeded": seeded, "backfill_complete": complete}


def _expiry(target, assessment, now):
    boundaries = [now + MAX_AGE]
    if target.get("evidence_valid_until"):
        boundaries.append(datetime.fromisoformat(target["evidence_valid_until"]))
    for event in target.get("signal_events", []):
        for field in ("expires_at", "observed_at", "effective_at", "refresh_at"):
            try:
                value = datetime.fromisoformat(event.get(field) or "")
                if value.tzinfo and value > now:
                    boundaries.append(value)
            except (ValueError, TypeError):
                pass
    for item in assessment.get("criteria", []):
        for evidence in item.get("evidence", []):
            for field in ("expires_at", "observed_at"):
                try:
                    value = datetime.fromisoformat(evidence.get(field) or "")
                    if field == "observed_at":
                        # Buyer evidence is fresh through the exact 90-day instant.
                        value += timedelta(days=90, microseconds=1)
                    if value.tzinfo and value > now:
                        boundaries.append(value)
                except (ValueError, TypeError):
                    pass
    return min(boundaries).isoformat()


def _evaluate_company(service, company_key, now):
    """Calculate a complete publication from the caller’s database snapshot."""
    connection = service.connection
    # A single latest identity representative is used for queue AND detail. Exclusions
    # belong to the company, including exclusions recorded on another current alias.
    row = connection.execute(
        "SELECT m.*,c.source_apex,c.hostname,c.label_hint FROM engine_ranking_members m "
        "JOIN candidates c ON c.id=m.candidate_id "
        "JOIN snapshots s ON s.id=m.snapshot_id "
        "JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
        "WHERE m.company_key=? ORDER BY ti.live DESC,s.observed_at DESC,m.snapshot_id DESC,"
        "m.candidate_id LIMIT 1",
        (company_key,),
    ).fetchone()
    if row is None:
        return None
    cid = row["candidate_id"]
    snapshot = service.store.scoring_snapshot(cid, reference_time=now)
    observation = json.loads(snapshot["observation_json"])
    if observation.get("live"):
        target = service._current_target(cid, {}, now)
    else:
        # Unavailable observations are unknown, not negative buyer evidence. Preserve
        # inspected identity/search/buyer controls without expensive dead-site rescoring.
        target = {
            "canonical_domain": company_key,
            "priority_score": None,
            "company_role": {"role": "unknown"},
            "availability": "unavailable",
        }
    if not target:
        raise ValueError("latest observation is unavailable for ranking")
    actual_key = target["canonical_domain"]
    if actual_key != company_key:
        raise ValueError("ranking identity does not match current observation")
    context = service._buyer_context(cid, company_key)
    context.update(target=target, role=target.get("company_role", {}), suggested_opportunities=[])
    from .buyer import build_buyer_assessment
    from .priority import build_sales_priority

    facts = service.buyer_facts.list(company_key)
    assessment = build_buyer_assessment(context, facts, now=now)
    priority = build_sales_priority(target, assessment, now=now)
    excluded = (
        connection.execute(
            "SELECT 1 FROM engine_ranking_members m JOIN prospect_workflow w "
            "ON w.candidate_id=m.candidate_id WHERE m.company_key=? AND w.excluded=1 LIMIT 1",
            (company_key,),
        ).fetchone()
        is not None
    )
    suppressed = connection.execute(
        "SELECT target_is_suppressed(?,?,?)", (row["source_apex"], row["hostname"], company_key)
    ).fetchone()[0]
    role = (target.get("company_role") or {}).get("role", "unknown")
    eligible = (
        not excluded
        and not suppressed
        and bool(observation.get("live"))
        and not target.get("disqualified")
        and target.get("target_scope") != "out_of_scope"
        and target.get("relationship_lane") != "referral_partner"
        and role != "directory_or_awards"
        and assessment["readiness"] not in {"deprioritize", "nurture"}
    )
    if excluded or suppressed:
        target = {**target, "disqualified": True}
        priority = build_sales_priority(target, assessment, now=now)
    evidence_version = _evidence_version(connection, company_key)
    # Keep the normalized inventory once, outside wide ranking JSON. Full backend packets
    # read it on demand with the exact score/evidence identity; compact screens never load it.
    accounting = target.get("opportunity", {}).get("evidence_accounting", [])
    if "opportunity" in target:
        target = {**target, "opportunity": {
            **target["opportunity"], "evidence_accounting": [],
        }}
    from ..naming import concise_company_name

    name = concise_company_name(
        label_hint=row["label_hint"] or "",
        title=observation.get("title") or "",
        canonical_domain=company_key,
        hostname=row["hostname"],
    )
    item = {
        "candidate_id": cid,
        "company_name": name,
        "company_key": company_key,
        "company_role": target.get("company_role", {}),
        "cohort": "agency"
        if role == "production_agency"
        else "consumer"
        if role in {"consumer_brand", "consumer_app_game"}
        else "other",
        "priority": priority["score"],
        "sales_priority": priority,
        "buyer_readiness": assessment["readiness"],
        "confirmed_count": assessment["confirmed_count"],
        "buyer_confirmed_work": any(
            o.get("status") == "confirmed" for o in context["opportunities"]
        ),
        "observed_at": snapshot["observed_at"],
        "computed_at": now.isoformat(),
        "as_of": now.isoformat(),
        "policy_version": policy_version(),
        "evidence_version": evidence_version,
    }

    def encode(value):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    from .ranking_modes import prepare_lenses

    return (
        (
            company_key, cid, row["snapshot_id"], name, priority["score"], int(eligible),
            int(excluded), item["policy_version"], evidence_version, item["as_of"],
            _expiry(target, assessment, now), 0, encode(target), encode(assessment),
            encode(priority), encode(item),
        ),
        prepare_lenses(item, target, assessment, priority), accounting,
    )


def _write_company(connection, company_key, result, now, *, prepared_packets=False):
    """Apply a complete projection within the caller's publication transaction."""
    if result is None:
        connection.execute("DELETE FROM engine_company_ranking WHERE company_key=?", (company_key,))
        return
    values, lenses, accounting = result
    connection.execute(
        """INSERT INTO engine_company_ranking VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company_key) DO UPDATE SET candidate_id=excluded.candidate_id,
        snapshot_id=excluded.snapshot_id,company_name=excluded.company_name,score=excluded.score,
        eligible=excluded.eligible,excluded=excluded.excluded,policy_version=excluded.policy_version,
        evidence_version=excluded.evidence_version,as_of=excluded.as_of,
        valid_until=excluded.valid_until,dirty=0,target_json=excluded.target_json,
        assessment_json=excluded.assessment_json,priority_json=excluded.priority_json,
        item_json=excluded.item_json""",
        values,
    )
    from .ranking_modes import write_lenses

    write_lenses(connection, lenses)
    from . import evidence_accounting

    if prepared_packets:
        evidence_accounting.replace_prepared_packets(connection, company_key, accounting)
    else:
        policy, evidence_version = values[7:9]
        evidence_accounting.publish(
            connection, company_key, accounting,
            now=now, policy=policy, evidence_version=evidence_version,
        )


def _publish_company(service, company_key, now):
    result = _evaluate_company(service, company_key, now)
    _write_company(service.connection, company_key, result, now)


def _repair_invalid_identity(store, company_key):
    """Split invalid legacy canonical tags using original snapshot identities.

    Caller owns publication's transaction. Only derived identity columns change;
    each snapshot resolves independently, including history of later redirects.
    """
    if store._appearance_subject_domain(company_key) == company_key:
        return set()
    from ..models import DiscoveredHost, SiteObservation

    keys = set()
    rows = store.connection.execute(
        "SELECT ti.snapshot_id,s.observation_json,c.hostname,c.source_apex "
        "FROM snapshot_target_intelligence ti JOIN snapshots s ON s.id=ti.snapshot_id "
        "JOIN candidates c ON c.id=s.candidate_id WHERE ti.canonical_domain=?",
        (company_key,),
    ).fetchall()
    for row in rows:
        candidate = DiscoveredHost(hostname=row["hostname"], source_apex=row["source_apex"])
        observation = SiteObservation.model_validate_json(row["observation_json"])
        resolved = store._target_canonical_domain(candidate, observation)
        if not resolved:
            raise ValueError("stored observation has no valid company identity")
        store.connection.execute(
            "UPDATE snapshot_target_intelligence SET canonical_domain=? WHERE snapshot_id=?",
            (resolved, row["snapshot_id"]),
        )
        store.connection.execute(
            "UPDATE snapshot_signal_events SET company_key=? WHERE snapshot_id=?",
            (resolved, row["snapshot_id"]),
        )
        store.connection.execute(
            "UPDATE engine_ranking_members SET company_key=? WHERE snapshot_id=?",
            (resolved, row["snapshot_id"]),
        )
        keys.add(resolved)
    return keys


def select_pending(store, limit, now, *, prioritize_expired_scores=False):
    """Select due shared work using the existing backfill and expiry policy."""
    repository = SalesEngineRepository(store.connection)
    stamp = now.isoformat()
    with repository._transaction():
        backfill = seed_backfill(store, limit=limit, now=now)
        # Recover the visible leaders first, while reserving most of the batch for
        # chronological work. Previous scores decide scheduling only.
        priority_ids = []
        if prioritize_expired_scores and limit >= 4:
            priority_ids = [row[0] for row in store.connection.execute(
                "SELECT r.candidate_id FROM engine_company_ranking r "
                "INDEXED BY idx_engine_company_ranking_search "
                "WHERE r.policy_version=? AND r.dirty=0 AND r.score IS NOT NULL "
                "AND r.valid_until<=? AND NOT EXISTS (SELECT 1 FROM engine_ranking_pending p "
                "WHERE p.candidate_id=r.candidate_id AND p.due_at>?) "
                "ORDER BY r.score DESC,r.company_key LIMIT ?",
                (policy_version(), stamp, stamp, limit // 4),
            )]
        due = store.connection.execute(
            "SELECT r.candidate_id FROM engine_company_ranking r "
            "WHERE r.dirty=0 AND r.valid_until<=? AND NOT EXISTS ("
            "SELECT 1 FROM engine_ranking_pending p "
            "WHERE p.candidate_id=r.candidate_id AND p.due_at>?) "
            "ORDER BY r.valid_until LIMIT ?",
            (stamp, stamp, limit),
        ).fetchall()
        due_ids = list(dict.fromkeys([*priority_ids, *(r[0] for r in due)]))[:limit]
        enqueue(store, due_ids, now=now)
        # Mark expiry dirty before selecting work, so failures cannot publish expired scores.
        for candidate_id in due_ids:
            store.connection.execute(
                "UPDATE engine_company_ranking SET dirty=1 WHERE candidate_id=?", (candidate_id,)
            )
    priority_jobs = [
        store.connection.execute(
            "SELECT candidate_id,failures FROM engine_ranking_pending "
            "WHERE candidate_id=? AND due_at<=?", (cid, stamp),
        ).fetchone()
        for cid in priority_ids
    ]
    rows = store.connection.execute(
        "SELECT candidate_id,failures FROM engine_ranking_pending WHERE due_at<=? "
        "ORDER BY due_at,candidate_id LIMIT ?",
        (stamp, limit),
    ).fetchall()
    if priority_jobs:
        rows = list({
            row["candidate_id"]: row for row in [*priority_jobs, *rows] if row is not None
        }.values())[:limit]
    return rows, backfill


def prepare_identity(store, cid):
    """Refresh membership and return all affected old, current and repaired keys."""
    keys = {
        r[0]
        for r in store.connection.execute(
            "SELECT company_key FROM engine_ranking_members WHERE candidate_id=? "
            "UNION SELECT company_key FROM engine_company_ranking WHERE candidate_id=?",
            (cid, cid),
        )
    }
    latest = store.connection.execute(
        "SELECT ti.canonical_domain,s.id FROM snapshots s "
        "JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
        "WHERE s.candidate_id=? ORDER BY s.id DESC LIMIT 1",
        (cid,),
    ).fetchone()
    if latest:
        store.connection.execute(
            "INSERT INTO engine_ranking_members VALUES(?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "company_key=excluded.company_key,snapshot_id=excluded.snapshot_id "
            "WHERE company_key IS NOT excluded.company_key "
            "OR snapshot_id IS NOT excluded.snapshot_id",
            (cid, latest[0], latest[1]),
        )
        keys.add(latest[0])
    else:
        store.connection.execute(
            "DELETE FROM engine_ranking_members WHERE candidate_id=?", (cid,)
        )
    for key in tuple(keys):
        keys.update(_repair_invalid_identity(store, key))
    return keys


def fail_pending(connection, cid, current_job, attempt_revision, now, error_kind):
    """Schedule a retry only if this is still the failed queue item."""
    failures = current_job["failures"] + 1
    retry = now + timedelta(minutes=min(1440, 2 ** min(failures, 11)))
    connection.execute(
        "UPDATE engine_ranking_pending SET failures=?,due_at=?,error_kind=? "
        "WHERE candidate_id=? AND enqueued_at=? AND due_at=? AND failures=? "
        "AND COALESCE((SELECT revision FROM engine_ranking_revisions "
        "WHERE candidate_id=?),0)=?",
        (failures, retry.isoformat(), error_kind, cid,
         current_job["enqueued_at"], current_job["due_at"], current_job["failures"],
         cid, attempt_revision),
    )


def refresh_rankings(
    store, *, limit=100, now=None, prioritize_expired_scores=False, should_defer=None,
    on_progress=None,
) -> dict:
    """One resumable batch with per-company transactions and independent retry clocks."""
    from ..desktop_ranking.control import MAX_COMPANIES, control_for, submit

    limit = max(1, min(int(limit), 500))
    if (control := control_for(store)) is not None:
        # Each request remains a bounded snapshot. Preserve the caller's total limit.
        combined = None
        remaining = limit
        while remaining:
            size = min(remaining, MAX_COMPANIES)
            result = submit(
                store, control, "refresh", limit=size, prioritize=prioritize_expired_scores,
                now=now.isoformat() if now is not None else None,
            )
            if combined is None:
                combined = result
            else:
                for field in ("selected", "refreshed", "reused", "seeded"):
                    combined[field] += result[field]
                combined["errors"].extend(result["errors"])
                combined["backfill_complete"] = result["backfill_complete"]
            if on_progress is not None:
                on_progress(combined["selected"], limit)
            remaining -= size
            if result["selected"] < size or (should_defer is not None and should_defer()):
                break
        return {key: combined[key] for key in (
            "selected", "refreshed", "reused", "errors", "as_of", "version",
            "provider_requests", "seeded", "backfill_complete",
        )}

    from .service import SalesEngineService

    now = _time(now)
    # Service schema installation is metadata-only and creates no source observations.
    service = SalesEngineService(store)
    repository = SalesEngineRepository(store.connection)
    rows, backfill = select_pending(
        store, limit, now, prioritize_expired_scores=prioritize_expired_scores,
    )
    completed, reused, errors = 0, 0, []
    for checked, row in enumerate(rows):
        if on_progress is not None:
            on_progress(checked, len(rows))
        if should_defer is not None and should_defer():
            break
        cid = row["candidate_id"]
        if not store.connection.execute(
            "SELECT 1 FROM engine_ranking_pending WHERE candidate_id=?", (cid,)
        ).fetchone():
            continue
        current_job = None
        attempt_revision = None
        try:
            with repository._transaction():
                current_job = store.connection.execute(
                    "SELECT enqueued_at,due_at,failures FROM engine_ranking_pending "
                    "WHERE candidate_id=? AND due_at<=?",
                    (cid, now.isoformat()),
                ).fetchone()
                if current_job is None:
                    continue
                revision = store.connection.execute(
                    "SELECT revision FROM engine_ranking_revisions WHERE candidate_id=?", (cid,),
                ).fetchone()
                attempt_revision = revision[0] if revision else 0
                keys = prepare_identity(store, cid)
                published = False
                for key in keys:
                    if not _publication_is_current(store.connection, key, now, policy_version()):
                        _publish_company(service, key, now)
                        published = True
                # An alias job may also own a former company key. Leave its cheap
                # reuse check queued until that candidate's complete repair succeeds.
                store.connection.execute(
                    "DELETE FROM engine_ranking_pending WHERE candidate_id=?", (cid,)
                )
            if published:
                completed += 1
            else:
                reused += 1
        except Exception as exc:
            if current_job is None or attempt_revision is None:
                raise
            with repository._transaction():
                fail_pending(store.connection, cid, current_job, attempt_revision,
                             now, type(exc).__name__)
            errors.append({"candidate_id": cid, "error": type(exc).__name__})
    else:
        if on_progress is not None:
            on_progress(len(rows), len(rows))
    return {
        "selected": len(rows),
        "refreshed": completed,
        "reused": reused,
        "errors": errors,
        "as_of": now.isoformat(),
        "version": policy_version(),
        "provider_requests": 0,
        **backfill,
    }


def refresh_candidate_rankings(store, candidate_ids, *, now=None, reuse_current=False):
    """Ensure targeted scores are current and return the number newly published."""
    from ..desktop_ranking.control import MAX_COMPANIES, control_for, submit

    if (control := control_for(store)) is not None:
        ids = list(dict.fromkeys(int(value) for value in candidate_ids))
        published = 0
        for start in range(0, len(ids), MAX_COMPANIES):
            result = submit(
                store, control, "candidates", candidate_ids=ids[start:start + MAX_COMPANIES],
                reuse_current=reuse_current, now=now.isoformat() if now is not None else None,
            )
            if result["errors"]:
                raise RuntimeError("Some desktop rankings could not be published")
            published += result["shared_companies_refreshed"]
        return published

    from .service import SalesEngineService

    now = _time(now)
    service = SalesEngineService(store)
    repository = SalesEngineRepository(store.connection)
    keys = set()
    for cid in dict.fromkeys(int(value) for value in candidate_ids):
        keys.update(row[0] for row in store.connection.execute(
            "SELECT company_key FROM engine_ranking_members WHERE candidate_id=?", (cid,),
        ))
    published = 0
    for key in sorted(keys):
        with repository._transaction():
            affected = {key, *_repair_invalid_identity(store, key)}
            for affected_key in sorted(affected):
                if not reuse_current or not _publication_is_current(
                    store.connection, affected_key, now, policy_version(),
                ):
                    _publish_company(service, affected_key, now)
                    published += affected_key == key
                store.connection.execute(
                    "DELETE FROM engine_ranking_pending WHERE candidate_id IN "
                    "(SELECT candidate_id FROM engine_ranking_members WHERE company_key=?) "
                    "AND NOT EXISTS (SELECT 1 FROM engine_company_ranking r "
                    "WHERE r.candidate_id=engine_ranking_pending.candidate_id "
                    "AND r.company_key<>?)",
                    (affected_key, affected_key),
                )
    return published


def company_projection(store, company_key, *, now=None):
    now = _time(now)
    row = store.connection.execute(
        "SELECT * FROM engine_company_ranking WHERE company_key=? AND policy_version=? "
        "AND dirty=0 AND valid_until>? AND as_of<=?",
        (company_key, policy_version(), now.isoformat(), now.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return {
        **dict(row),
        "target": json.loads(row["target_json"]),
        "assessment": json.loads(row["assessment_json"]),
        "priority": json.loads(row["priority_json"]),
    }


def list_rankings(store, *, tag="", search="", limit=30, prospects_only=False, now=None,
                  offset=0, mode="balanced"):
    """Full packets share the compact path's exact page membership and order."""
    from .ranking_modes import stored_priority
    from .summaries import _snapshot, compact_queue

    now = _time(now)
    with _snapshot(store.connection):
        result = compact_queue(store, search=search, tag=tag, limit=limit,
                               prospects_only=prospects_only,
                               reference_time=now, offset=offset, mode=mode)
        items = []
        for item in result["items"]:
            if item["ranking_state"] == "current":
                row = store.connection.execute(
                    "SELECT item_json FROM engine_company_ranking WHERE company_key=?",
                    (item["company_key"],),
                ).fetchone()
                full = json.loads(row[0])
                priority = stored_priority(store.connection, item["company_key"], mode,
                                           full["sales_priority"], now=now)
            else:
                full = {**item, "computed_at": None}
                priority = {"score": None, "label": "Unscored", "factors": [], "reason": "",
                            "limits": [], "version": policy_version()}
            items.append({**full, "priority": priority["score"], "sales_priority": priority})
        state = store.connection.execute(
            "SELECT last_candidate_id FROM engine_ranking_backfill WHERE policy_version=?",
            (policy_version(),),
        ).fetchone()
    return {
        **result,
        "items": items,
        "selection_basis": "Current canonical Sales priority across the projected inventory",
        "coverage": {
            **result["coverage"],
            "last_candidate_id": state[0] if state else 0,
            "as_of": now.isoformat(),
            "uninspected_state": "unknown",
            "candidates_assessed": 0,
            "maximum_candidates": 0,
            "limit_reached": False,
        },
    }
