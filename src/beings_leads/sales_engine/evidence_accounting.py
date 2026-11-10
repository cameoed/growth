"""Exhaustive derived input inventory. Raw observations remain in their original ledgers.

Accounting stores references, hashes, roles and dispositions, never duplicate provider payloads.
An unmapped datum remains an explicit research gap rather than silently receiving points.
"""

import hashlib
import json
import zlib
from collections import Counter, defaultdict, deque
from contextlib import suppress
from datetime import UTC, datetime

from ..storage_json import configure, decode_packet, encode_packet

VERSION = "evidence-accounting-v2-final"
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS engine_evidence_accounting (
        company_key TEXT NOT NULL, input_key TEXT NOT NULL, source_kind TEXT NOT NULL,
        role TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL,
        as_of TEXT NOT NULL, policy_version TEXT NOT NULL, evidence_version TEXT NOT NULL,
        record_json TEXT NOT NULL, PRIMARY KEY(company_key,input_key))""",
    """CREATE INDEX IF NOT EXISTS idx_engine_accounting_disposition
        ON engine_evidence_accounting(company_key,disposition,role)""",
)

LEDGERS = {
    "buyer_evidence": "buyer_evidence",
    "engine_workload_inputs": "saved_work",
    "engine_creative_annotations": "creative_annotations",
    "engine_concept_labels": "creative_annotations",
    "engine_relationships": "relationships",
    "engine_buyer_facts": "buyer_controls",
    "engine_research_reviews": "research_allocation",
    "engine_opportunities": "buyer_controls",
    "company_email_lookups": "contacts",
    "market_events": "market_identity",
    "market_documents": "market_identity",
    "market_entity_mentions": "market_identity",
    "company_relationships": "identity",
    "ad_creatives": "advertising",
    "sales_accounts": "outcome_evaluation",
    "sales_account_events": "buyer_controls",
    "account_claims": "buyer_controls",
    "candidate_source_edges": "discovery",
    "candidate_ad_observations": "advertising",
    "advertiser_domain_links": "advertising_identity",
    "advertiser_entities": "advertising_identity",
    "ad_discovery_runs": "research_allocation",
    "intelligence_feature_snapshots": "derived_history",
    "prospect_workflow": "buyer_controls",
}


def ledger_where(table, predicates):
    """Keep each identity lookup indexable, then deduplicate the physical receipts."""
    if len(predicates) == 1:
        return predicates[0]
    rowids = " UNION ".join(f"SELECT rowid FROM {table} WHERE {where}" for where in predicates)
    return f"rowid IN ({rowids})"


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# SQLite supports mixed TEXT/BLOB values: old databases remain readable during migration.
# A tagged envelope avoids mistaking arbitrary bytes for our storage format.
_STORAGE_TAG = b"EAC1\x00"


def encode_record(record):
    raw = _encode(record)
    packed = _STORAGE_TAG + zlib.compress(raw.encode("utf-8"), level=6)
    return packed if len(packed) < len(raw.encode("utf-8")) else raw


def decode_record(value):
    if isinstance(value, bytes):
        if not value.startswith(_STORAGE_TAG):
            raise ValueError("Unknown evidence accounting storage format")
        return zlib.decompress(value[len(_STORAGE_TAG):]).decode("utf-8")
    return value


PACKET_ROWS = 256
PACKET_BYTES = 256 * 1024
LEGACY = "engine_evidence_accounting_legacy"
PACKETS = "engine_evidence_packets"
COLUMN_NAMES = ("company_key", "input_key", "source_kind", "role", "disposition", "reason",
                "as_of", "policy_version", "evidence_version", "record_json")


def initialize_storage(connection):
    """Install a mixed-format logical ledger; never backfill on application startup."""
    configure(connection, rows=False)
    existing = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='engine_evidence_accounting'"
    ).fetchone()
    if existing and existing[0] == "view":
        return
    connection.execute("SAVEPOINT accounting_storage_schema")
    try:
        legacy_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEGACY,)
        ).fetchone()
        if existing is None and not legacy_exists:
            for statement in SCHEMA:
                connection.execute(statement)
        if not legacy_exists:
            connection.execute(f"ALTER TABLE engine_evidence_accounting RENAME TO {LEGACY}")
        elif existing is not None:
            raise ValueError("Both legacy and unmigrated accounting tables exist")
        connection.execute(f"CREATE TABLE IF NOT EXISTS {PACKETS}(company_key TEXT NOT NULL,"
                           "chunk INTEGER NOT NULL,record_count INTEGER NOT NULL,"
                           "records BLOB NOT NULL,PRIMARY KEY(company_key,chunk))")
        projected = ",".join(
            f"json_extract(entry.value,'$[{i}]') AS {name}"
            for i, name in enumerate(COLUMN_NAMES[1:])
        )
        connection.execute(
            "CREATE VIEW engine_evidence_accounting AS "
            f"SELECT * FROM {LEGACY} UNION ALL SELECT packet.company_key,{projected} "
            f"FROM {PACKETS} packet,json_each(storage_json(packet.records)) entry"
        )
        connection.execute("RELEASE accounting_storage_schema")
    except BaseException:
        connection.execute("ROLLBACK TO accounting_storage_schema")
        connection.execute("RELEASE accounting_storage_schema")
        raise


def packet_rows(company, rows):
    """Bound each packet without dropping inputs or limiting a company's evidence."""
    batch, size, chunk = [], 0, 0
    for row in rows:
        values = list(row[1:])
        values[-1] = decode_record(values[-1])
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        encoded_size = len(encoded.encode("utf-8"))
        if batch and (len(batch) >= PACKET_ROWS or size + encoded_size > PACKET_BYTES):
            yield company, chunk, len(batch), encode_packet("[" + ",".join(batch) + "]")
            batch, size, chunk = [], 0, chunk + 1
        batch.append(encoded)
        size += encoded_size
    if batch:
        yield company, chunk, len(batch), encode_packet("[" + ",".join(batch) + "]")


def replace_packets(connection, company, rows):
    replace_prepared_packets(connection, company, packet_rows(company, rows))


def replace_prepared_packets(connection, company, prepared):
    """Caller owns the transaction; readers see either complete publication."""
    last_chunk = -1

    def packets():
        nonlocal last_chunk
        for packet in prepared:
            last_chunk = packet[1]
            yield packet

    connection.executemany(
        f"INSERT INTO {PACKETS} VALUES(?,?,?,?) ON CONFLICT(company_key,chunk) "
        "DO UPDATE SET record_count=excluded.record_count,records=excluded.records "
        "WHERE records IS NOT excluded.records OR record_count IS NOT excluded.record_count",
        packets(),
    )
    connection.execute(f"DELETE FROM {PACKETS} WHERE company_key=? AND chunk>?",
                       (company, last_chunk))
    connection.execute(f"DELETE FROM {LEGACY} WHERE company_key=?", (company,))


def migrate_company(connection, company, *, validate_record=lambda value: None):
    """Move original logical rows without changing any publication metadata."""
    if connection.execute(f"SELECT 1 FROM {PACKETS} WHERE company_key=? LIMIT 1",
                          (company,)).fetchone():
        raise ValueError("Company has both legacy and packet accounting")
    rows = connection.execute(f"SELECT * FROM {LEGACY} WHERE company_key=? ORDER BY input_key",
                              (company,))
    count, digest = 0, hashlib.sha256()

    def originals():
        nonlocal count
        for row in rows:
            logical = (*row[:-1], decode_record(row[-1]))
            validate_record(logical[-1])
            digest.update((_encode(logical) + "\n").encode("utf-8"))
            count += 1
            yield logical

    connection.executemany(f"INSERT INTO {PACKETS} VALUES(?,?,?,?)",
                           packet_rows(company, originals()))
    verified, actual = 0, hashlib.sha256()
    for (packed,) in connection.execute(f"SELECT records FROM {PACKETS} WHERE company_key=? "
                                        "ORDER BY chunk", (company,)):
        for row in json.loads(decode_packet(packed)):
            actual.update((_encode((company, *row)) + "\n").encode("utf-8"))
            verified += 1
    if verified != count or actual.digest() != digest.digest():
        raise ValueError("Accounting packet verification failed")
    connection.execute(f"DELETE FROM {LEGACY} WHERE company_key=?", (company,))
    return count, digest.hexdigest()


def _stamp(value):
    with suppress(ValueError, TypeError):
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo:
            return stamp.astimezone(UTC)
    return None


def _leaves(value, path=""):
    if isinstance(value, dict) and value:
        for key, child in sorted(value.items()):
            yield from _leaves(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, f"{path}[{index}]")
    else:
        yield path, value


def _field_role(path):
    # Closed explicit source roles; new fields are deliberately flagged for integration.
    root = path.split(".")[0].split("[")[0]
    if root in {"hostname", "canonical_host", "final_url", "title", "description", "tenant_label"}:
        return "identity_fit"
    if root in {"live", "status_code", "language", "error", "fetch_outcome", "fetch_disposition"}:
        return "risk"
    if root in {"emails", "contacts", "contact_urls", "social_urls"}:
        return "access"
    if root in {"signals", "text_excerpt", "pages", "technologies", "public_ids"}:
        return "fit_demand_capacity_risk"
    if path.startswith("web."):
        name = path.split(".")[1].split("[")[0]
        if name in {"vendors", "sales"}:
            return "demand_distribution_capacity"
        if name in {
            "sitemap_recent_30d",
            "sitemap_recent_90d",
            "sitemap_recent_counts",
            "latest_sitemap_change",
        }:
            return "historical_demand_dated_timing"
        if name in {
            "open_jobs",
            "creative_jobs",
            "photo_video_jobs",
            "growth_jobs",
            "leadership_jobs",
            "job_titles",
            "job_postings",
            "ats_boards",
            "career_urls",
            "career_url_sources",
        }:
            return "production_capacity_dated_timing"
        if name in {
            "product_objects",
            "offer_objects",
            "organization_objects",
            "location_objects",
            "max_review_count",
            "sitemap_total_urls",
            "sitemap_counts",
            "common_crawl_pages",
            "portfolio_domains",
        }:
            return "fit_operating_capacity"
        if name == "company_appearances":
            return "identity_referral_research"
        if name in {
            "jsonld_types",
            "sitemaps_checked",
            "source_urls",
            "errors",
            "common_crawl_latest",
            "common_crawl_collection",
        }:
            return "coverage_provenance"
    if root in {
        "observed_at",
        "content_hash",
        "evidence_urls",
        "legal_urls",
        "outbound_domains",
        "fetch_attempts",
        "recovered_after_retry",
        "fetch_provider",
        "fallback_attempted",
        "fallback_proxy_used",
        "fallback_error",
        "canonical_recovery_required",
    }:
        return "coverage_provenance"
    return "unmapped_research"


def build_accounting(store, company, snapshot_row, observation, score, *, now):
    events = score.creative_capacity.signal_events
    dominant = {}
    for event in events:
        root = (event.family, event.independence_key)
        previous = dominant.get(root)
        if previous is None or (event.current_strength, event.event_key) > (
            previous.current_strength,
            previous.event_key,
        ):
            dominant[root] = event
    gate_reasons = {
        r["input_key"]: r["reason"] for r in score.creative_capacity.opportunity.evidence_accounting
    }
    rows = []

    def add(key, source, role, value, *, reason=None, linked=(), reference=None):
        active = [e for e in linked if e.current_strength > 0]
        roots = {(e.family, e.independence_key) for e in active}
        if reason in {
            "wrong_company",
            "incomplete_provenance",
            "missing_exact_artifact_lineage",
            "future_record",
            "malformed_payload",
        }:
            disposition = "rejected"
        elif active:
            disposition = (
                "scoring_input"
                if any(
                    dominant[(e.family, e.independence_key)].event_key == e.event_key
                    for e in active
                )
                else "corroboration"
            )
            reason = reason or "accepted_causal_evidence"
        elif linked:
            disposition, reason = "expired_or_inactive", reason or "no_current_signal_survives"
        elif reason in {"withdrawn_record", "expired_record"}:
            disposition = "withdrawn" if reason == "withdrawn_record" else "expired_or_inactive"
        elif reason and reason.startswith("superseded"):
            disposition = "superseded"
        elif reason and reason.startswith("invalidity:"):
            disposition = "rejected"
        elif role in {"buyer_controls", "outcome_evaluation", "research_allocation"}:
            disposition, reason = role, reason or "separate_from_public_purchase_intent"
        elif reason == "aggregate_input_source_effect_measured":
            disposition = "aggregate_input"
        elif value is None or value == [] or value == {} or value == "":
            disposition, reason = "unknown", reason or "missing_fact"
        else:
            disposition = "research_coverage"
            reason = reason or "retained_context_no_independent_score_event"
        rows.append(
            {
                "input_key": key,
                "source_kind": source,
                "role": role,
                "disposition": disposition,
                "reason": reason,
                "source_reference": reference or key,
                "value_hash": hashlib.sha256(_encode(value).encode()).hexdigest(),
                "event_keys": sorted({e.event_key for e in linked}),
                "causal_roots": sorted(f"{family}|{root}" for family, root in roots),
            }
        )

    raw = json.loads(snapshot_row["observation_json"])
    for key in ("buyer_evidence", "paid_creative_asset_keys"):
        raw.pop(key, None)
    raw.get("web", {}).pop("frontier", None)
    for path, value in _leaves(raw):
        role = _field_role(path)
        # Aggregate fields are measured by the full website-removal replay rather than
        # assigned invented per-leaf points. This association is explicitly source-level.
        add(
            f"snapshot:{snapshot_row['snapshot_id']}:{path}",
            "website",
            role,
            value,
            reason=(
                "unmapped_field_requires_policy"
                if role == "unmapped_research"
                else "aggregate_input_source_effect_measured"
                if role not in {"coverage_provenance", "identity_referral_research"}
                and value not in (None, "", [], {})
                else None
            ),
        )

    frontier = observation.web.frontier
    # Logical keys may repeat across executions. Accounting inventories physical receipts;
    # event/gate linkage continues to use the logical key and causal scoring stays unchanged.
    receipt_ids = defaultdict(deque)
    execution_ids = [e.execution_id for e in frontier.executions if e.execution_id is not None]
    if execution_ids:
        marks = ",".join("?" for _ in execution_ids)
        for table, key, date in (
            ("atomic_observations", "observation_key", "first_observed_at"),
            ("evidence_artifacts", "artifact_key", None),
            ("coverage_manifests", "manifest_key", "observed_at"),
        ):
            date_filter = f" AND {date}<=?" if date else ""
            args = [*execution_ids, now.isoformat()]
            if date:
                args.append(now.isoformat())
            for receipt in store.connection.execute(
                f"SELECT id,execution_id,{key} FROM {table} "
                f"INDEXED BY sqlite_autoindex_{table}_1 WHERE execution_id IN ({marks}) "
                f"AND recorded_at<=?{date_filter} ORDER BY recorded_at,id",
                args,
            ):
                receipt_ids[(table, receipt[key])].append(dict(receipt))

    def receipt_reference(table, key, record, index):
        logical = getattr(record, key)
        receipts = receipt_ids[(table, logical)]
        if receipts:
            receipt = receipts.popleft()
            return f"{logical}:receipt:{receipt['id']}", {"table": table, **receipt}
        # Inline evidence has no ledger ID. Keep every occurrence with a content reference.
        digest = hashlib.sha256(_encode(record.model_dump(mode="json")).encode()).hexdigest()
        return f"{logical}:inline:{digest}:{index}", {"key": logical, "value_hash": digest}

    for index, atomic in enumerate(frontier.observations):
        identity, reference = receipt_reference(
            "atomic_observations", "observation_key", atomic, index
        )
        linked = [e for e in events if e.details.get("observation_key") == atomic.observation_key]
        if atomic.predicate == "contact.provider_professional_person":
            value = atomic.value if isinstance(atomic.value, dict) else {}
            linked = [
                e
                for e in events
                if e.event_type == "hydrated_buyer_contact"
                and e.details.get("person_profile") == value.get("linkedin_url")
            ]
        add(
            "atomic:" + identity,
            "frontier",
            ("first_party_fit_demand_timing_access" if atomic.predicate == "microlink.page"
             else "research_coverage" if atomic.predicate == "microlink.search_results"
             else atomic.predicate),
            atomic.model_dump(mode="json"),
            linked=linked,
            reason=(
                "accepted_current_professional_route"
                if atomic.predicate == "contact.provider_professional_person" and linked
                else gate_reasons.get("atomic:" + atomic.observation_key)
            ),
            reference={
                **reference,
                "source_url": atomic.source_url,
            },
        )
    for collection, key, table in (
        (frontier.evidence_artifacts, "artifact_key", "evidence_artifacts"),
        (frontier.coverage_manifests, "manifest_key", "coverage_manifests"),
        (frontier.executions, "execution_id", None),
    ):
        for index, record in enumerate(collection):
            if table:
                identity, reference = receipt_reference(table, key, record, index)
            else:
                identity = getattr(record, key, None) or f"{record.scanner_id}:{index}"
                reference = {"key": identity}
            add(
                f"{key}:{identity}",
                "frontier",
                "coverage_provenance",
                record.model_dump(mode="json"),
                reference=reference,
            )
    for path, value in _leaves(frontier.features):
        add(
            f"feature:{path}",
            "frontier",
            "typed_feature_projection",
            value,
            reason="derived_feature_contract_checked_by_scorer",
        )
    for index, decision in enumerate(frontier.schedule_decisions):
        add(
            f"schedule:{decision.scanner_id}:{index}",
            "frontier",
            "research_allocation",
            decision.model_dump(mode="json"),
        )
    for field in list(snapshot_row.keys()):
        if field != "observation_json":
            add(
                f"candidate:{snapshot_row['snapshot_id']}:{field}",
                "discovery",
                "identity_provenance",
                snapshot_row[field],
            )

    tables = {
        r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    selected = [(table, source) for table, source in LEDGERS.items() if table in tables]
    prefetch = getattr(store.connection, "prefetch", None)
    if prefetch is not None:
        prefetch((f"PRAGMA table_info({table})", ()) for table, _ in selected)
    queries = []
    for table, source in selected:
        info = store.connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {r[1] for r in info}
        matches = [
            name
            for name in (
                "company_key",
                "subject_company_key",
                "object_company_key",
                "website_host",
                "landing_domain",
                "direct_company_key",
                "domain",
            )
            if name in columns
        ]
        conditions = [f"{name}=?" for name in matches]
        args = [company] * len(matches)
        for name in ("subject_key", "object_key"):
            if name in columns:
                conditions.append(f"{name} IN (?,?)")
                args.extend([company, "domain:" + company])
        if "candidate_id" in columns:
            conditions.append(
                "candidate_id IN (SELECT candidate_id FROM engine_ranking_members "
                "WHERE company_key=? UNION SELECT candidate_id FROM snapshots "
                "WHERE id=?)"
            )
            args.extend([company, snapshot_row["snapshot_id"]])
        if "account_id" in columns and "sales_accounts" in tables:
            conditions.append("account_id IN (SELECT id FROM sales_accounts WHERE website_host=?)")
            args.append(company)
        if table == "advertiser_entities" and "advertiser_domain_links" in tables:
            conditions.append(
                "id IN (SELECT advertiser_id FROM advertiser_domain_links WHERE domain=?)"
            )
            args.append(company)
        if table == "ad_discovery_runs" and "candidate_ad_observations" in tables:
            conditions.append(
                "id IN (SELECT run_id FROM candidate_ad_observations WHERE "
                "candidate_id IN (SELECT candidate_id FROM engine_ranking_members "
                "WHERE company_key=? UNION SELECT candidate_id FROM snapshots "
                "WHERE id=?))"
            )
            args.extend([company, snapshot_row["snapshot_id"]])
        if table == "market_documents" and "market_entity_mentions" in tables:
            conditions.append(
                "id IN (SELECT document_id FROM market_entity_mentions WHERE company_key=?)"
            )
            args.append(company)
        if table == "market_events" and "market_entity_mentions" in tables:
            conditions.append(
                "mention_id IN (SELECT id FROM market_entity_mentions WHERE company_key=?)"
            )
            args.append(company)
        if not conditions:
            continue
        where = ledger_where(table, conditions)
        queries.append((table, source, info, f"SELECT * FROM {table} WHERE {where}", args))
    if prefetch is not None:
        prefetch((sql, args) for _, _, _, sql, args in queries)
    for table, source, info, sql, args in queries:
        records = store.connection.execute(sql, args).fetchall()
        latest_buyer = {}
        if table == "buyer_evidence":
            for row in records:
                key = row["kind"], row["record_key"]
                if row["observed_at"] <= now.isoformat() and (
                    key not in latest_buyer
                    or (row["observed_at"], row["id"])
                    > (latest_buyer[key]["observed_at"], latest_buyer[key]["id"])
                ):
                    latest_buyer[key] = row
        for row in records:
            value = dict(row)
            primary = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            identity = (
                str(value["id"])
                if "id" in value
                else hashlib.sha256(
                    _encode({k: value[k] for k in primary} or value).encode()
                ).hexdigest()[:24]
            )
            payload = None
            for field in ("payload_json", "annotation_json", "labels_json"):
                if value.get(field):
                    with suppress(TypeError, ValueError):
                        payload = json.loads(value[field])
                    break
            linked = []
            if isinstance(payload, dict):
                record_key = payload.get("record_key") or payload.get("event_key")
                linked = [
                    e
                    for e in events
                    if (
                        record_key
                        and (
                            e.details.get("event_key") == record_key
                            or record_key in json.loads(e.details.get("source_records", "[]"))
                        )
                    )
                ]
                if table == "engine_creative_annotations":
                    linked = [
                        e
                        for e in events
                        if payload.get("asset_key")
                        in json.loads(e.details.get("annotation_assets", "[]"))
                    ]
                if table == "engine_workload_inputs" and "scope" in payload:
                    linked = [
                        e
                        for e in events
                        if e.details.get("scope") == payload["scope"]
                        and _stamp(payload.get("observed_at"))
                        in [_stamp(d) for d in json.loads(e.details.get("comparison_dates", "[]"))]
                    ]
                if table == "engine_relationships":
                    linked = [e for e in events if e.details.get("relationship_id") == identity]
                if table == "engine_concept_labels":
                    linked = [e for e in events if e.event_type == "hydrated_validated_creative"]
            reason = "malformed_payload" if payload is None and value.get("payload_json") else None
            if table == "buyer_evidence":
                latest = latest_buyer.get((value["kind"], value["record_key"]))
                if latest is None or latest["id"] != value["id"]:
                    reason, linked = "superseded_logical_record", []
                elif isinstance(payload, dict) and not linked:
                    if payload.get("current") is False:
                        reason = "withdrawn_record"
                    elif not payload.get("identity_verified"):
                        reason = "unverified_identity"
                    elif not _stamp(payload.get("effective_at")):
                        reason = "undated_fact"
                    elif (
                        _stamp(payload.get("expires_at"))
                        and _stamp(payload["expires_at"]) <= now
                        or (
                            payload.get("kind") not in {"advertising", "creator"}
                            and _stamp(payload.get("observed_at"))
                            and (now - _stamp(payload["observed_at"])).days > 90
                        )
                    ):
                        reason = "expired_record"
            if isinstance(payload, dict) and payload.get("company_key") not in {None, company}:
                reason, linked = "wrong_company", []
            stamp = value.get("observed_at") or value.get("recorded_at") or value.get("created_at")
            if stamp:
                with suppress(ValueError, TypeError):
                    parsed = datetime.fromisoformat(str(stamp))
                    if parsed.tzinfo and parsed.astimezone(UTC) > now:
                        reason, linked = "future_record", []
            add(
                f"{table}:{identity}",
                source,
                source,
                payload or value,
                linked=linked,
                reason=reason,
                reference={"table": table, "id": identity},
            )
    # Original historical snapshots are retained as comparisons, never labelled active work.
    for row in store.connection.execute(
        "SELECT s.id,s.observed_at FROM snapshots s JOIN snapshot_target_intelligence t "
        "ON t.snapshot_id=s.id WHERE t.canonical_domain=? AND s.observed_at<=? AND s.id<>?",
        (company, now.isoformat(), snapshot_row["snapshot_id"]),
    ):
        add(
            f"history:{row['id']}",
            "website_history",
            "temporal_comparison",
            dict(row),
            reason="immutable_history_comparison_or_archive",
            reference={"snapshot_id": row["id"]},
        )
    return sorted(rows, key=lambda r: r["input_key"])


def prepare_packets(company, accounting, *, now, policy, evidence_version):
    stamp = now.isoformat()
    return packet_rows(company, (
        (company, row["input_key"], row["source_kind"], row["role"], row["disposition"],
         row["reason"], stamp, policy, evidence_version, _encode(row))
        for row in accounting
    ))


def publish(connection, company, accounting, *, now, policy, evidence_version):
    # Caller owns the ranking transaction. A failure rolls score and accounting back together.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (PACKETS,)).fetchone():
        replace_prepared_packets(connection, company, prepare_packets(
            company, accounting, now=now, policy=policy, evidence_version=evidence_version,
        ))
        return
    # Reuse the indexed rows. Remove only withdrawn keys; update publication metadata
    # and changed payloads in place, rather than rebuilding both indexes for every input.
    # A TEMP key set avoids SQLite parameter limits without capping the exhaustive ledger.
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS accounting_publish_keys("
                       "input_key TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.execute("DELETE FROM accounting_publish_keys")
    connection.executemany("INSERT INTO accounting_publish_keys VALUES(?)",
                           ((r["input_key"],) for r in accounting))
    connection.execute("DELETE FROM engine_evidence_accounting WHERE company_key=? "
                       "AND input_key NOT IN (SELECT input_key FROM accounting_publish_keys)",
                       (company,))
    connection.executemany(
        "INSERT INTO engine_evidence_accounting VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(company_key,input_key) DO UPDATE SET "
        "source_kind=excluded.source_kind,role=excluded.role,disposition=excluded.disposition,"
        "reason=excluded.reason,as_of=excluded.as_of,policy_version=excluded.policy_version,"
        "evidence_version=excluded.evidence_version,record_json=excluded.record_json "
        "WHERE source_kind IS NOT excluded.source_kind OR role IS NOT excluded.role "
        "OR disposition IS NOT excluded.disposition OR reason IS NOT excluded.reason "
        "OR record_json IS NOT excluded.record_json",
        [
            (
                company,
                r["input_key"],
                r["source_kind"],
                r["role"],
                r["disposition"],
                r["reason"],
                now.isoformat(),
                policy,
                evidence_version,
                encode_record(r),
            )
            for r in accounting
        ],
    )
    # Updating only non-indexed publication fields avoids rewriting the disposition/role
    # index for unchanged inputs while retaining the exact current publication metadata.
    stamp = now.isoformat()
    connection.execute(
        "UPDATE engine_evidence_accounting SET as_of=?,policy_version=?,evidence_version=? "
        "WHERE company_key=? AND (as_of IS NOT ? OR policy_version IS NOT ? "
        "OR evidence_version IS NOT ?)",
        (stamp, policy, evidence_version, company, stamp, policy, evidence_version),
    )


def summary(accounting):
    return {
        "version": VERSION,
        "input_count": len(accounting),
        "dispositions": dict(sorted(Counter(r["disposition"] for r in accounting).items())),
    }
