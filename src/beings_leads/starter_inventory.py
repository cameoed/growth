"""Install dated public observations once, when creating a dashboard database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .domain_policy import is_disallowed
from .models import DiscoveredHost, SiteObservation
from .sales_engine.ranking import refresh_candidate_rankings
from .scoring import score_candidate
from .storage_maintenance import enable_incremental_vacuum_for_empty_store
from .store import LeadStore

STARTER_PACK = "bundled_public_starter_inventory_v1"
PENDING = -1


def _needs_starter_pack(connection) -> bool:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        row = connection.execute(
            "SELECT rows_changed FROM schema_migrations WHERE name=?", (STARTER_PACK,),
        ).fetchone()
        return row is not None and row[0] == PENDING
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is None


def _reserve_new_store(path: Path) -> bool:
    with LeadStore(path, initialize=False) as store:
        connection = store.connection
        if not _needs_starter_pack(connection):
            return False
        enable_incremental_vacuum_for_empty_store(connection)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if not _needs_starter_pack(connection):
                return False
            connection.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    rows_changed INTEGER NOT NULL DEFAULT 0
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?, ?, ?)",
                (STARTER_PACK, datetime.now(UTC).isoformat(), PENDING),
            )
    return True


def _load_observations() -> list[SiteObservation]:
    return [
        SiteObservation.model_validate(row)
        for row in json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
    ]


def initialize_starter_inventory(path: Path) -> int:
    if not _reserve_new_store(path):
        return 0
    with LeadStore(path) as store, store.connection as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _needs_starter_pack(connection):
            return 0
        # A separate importer can acquire the store between reservation and schema setup.
        existing_company = connection.execute("SELECT 1 FROM candidates LIMIT 1").fetchone()
        count = 0 if existing_company else _install_observations(store, _load_observations())
        connection.execute(
            "UPDATE schema_migrations SET rows_changed=?, applied_at=? WHERE name=?",
            (count, datetime.now(UTC).isoformat(), STARTER_PACK),
        )
        return count


def _install_observations(store: LeadStore, observations: list[SiteObservation]) -> int:
    observations = [
        item for item in observations
        if not any(is_disallowed(value) for value in (
            item.hostname, item.canonical_host or item.hostname,
            item.final_url or item.hostname, *item.evidence_urls,
        ))
    ]
    candidates = [
        DiscoveredHost(
            source_apex="starter-inventory",
            hostname=item.hostname,
            label_hint=item.title,
            discovery_evidence_urls=item.evidence_urls,
        )
        for item in observations
    ]
    if not store.upsert_candidates(candidates, commit=False):
        return 0
    ids = store.candidate_ids([candidate.hostname for candidate in candidates])
    first_observed = min(item.observed_at for item in observations).isoformat()
    last_observed = max(item.observed_at for item in observations).isoformat()
    connection = store.connection
    receipt = connection.execute(
        "INSERT INTO runs(started_at,finished_at,status,config_json,summary_json) "
        "VALUES (?,?,'complete',?,?)",
        (first_observed, last_observed, json.dumps({"source": STARTER_PACK}),
         json.dumps({"inspected": len(ids), "source": "bundled public observations"})),
    )
    run_id = int(receipt.lastrowid)
    connection.execute(
        "INSERT INTO run_progress(run_id,phase,discovered,inspected,total_candidates,updated_at) "
        "VALUES (?,'complete',?,?,?,?)",
        (run_id, len(ids), len(ids), len(ids), last_observed),
    )
    for candidate, observation in zip(candidates, observations, strict=True):
        if candidate_id := ids.get(candidate.hostname):
            score = score_candidate(candidate, observation)
            store.save_snapshot(run_id, candidate_id, observation, score, None, score.total)
    refresh_candidate_rankings(store, ids.values())
    return len(ids)
