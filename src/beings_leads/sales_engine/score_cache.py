"""Bounded process cache invalidated by company evidence ledgers, never by UI activity."""

from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock

from .hydrated_scoring import VERSION

_CACHE = OrderedDict()
_LOCK = RLock()
MAX_ENTRIES = 256
TTL_SECONDS = 60


def evidence_key(store, candidate_id, company, snapshot_row, ads):
    # A transaction may see uncommitted evidence. Do not let it escape via a process cache.
    if store.connection.in_transaction:
        return None
    db_stat = store.path.stat()
    parts = [tuple(snapshot_row), ads]
    tables = {
        r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "engine_ranking_revisions" in tables:
        # Transactional triggers cover inserts, corrections, deletes, contacts,
        # aliases and buyer facts; MAX(id)/COUNT cannot detect in-place corrections.
        from .ranking import policy_version

        revisions = [tuple(r) for r in store.connection.execute(
            "SELECT m.candidate_id,COALESCE(v.revision,0) FROM engine_ranking_members m "
            "LEFT JOIN engine_ranking_revisions v ON v.candidate_id=m.candidate_id "
            "WHERE m.company_key=? ORDER BY m.candidate_id", (company,))]
        own = store.connection.execute(
            "SELECT revision FROM engine_ranking_revisions WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        digest = sha256(repr((parts, revisions, tuple(own or ()))).encode()).hexdigest()
        return (str(store.path.resolve()), db_stat.st_ino, candidate_id, policy_version(), digest)
    for table in (
        "buyer_evidence",
        "atomic_observations",
        "evidence_artifacts",
        "coverage_manifests",
        "intelligence_feature_snapshots",
        "engine_workload_inputs",
        "engine_creative_annotations",
    ):
        if table in tables:
            parts.append(
                (
                    table,
                    tuple(
                        store.connection.execute(
                            f"SELECT MAX(id), COUNT(*) FROM {table} WHERE company_key=?", (company,)
                        ).fetchone()
                    ),
                )
            )
    parts.append(
        [
            tuple(r)
            for r in store.connection.execute(
                "SELECT id,status,finished_at,observation_count FROM scanner_executions "
                "WHERE company_key=? "
                "ORDER BY started_at DESC,id DESC LIMIT 100",
                (company,),
            )
        ]
    )
    for table, query in (
        ("company_email_lookups",
         "SELECT id,status,observed_at,expires_at FROM company_email_lookups "
         "WHERE company_key=? ORDER BY id DESC LIMIT 100"),
        (
            "engine_concept_labels",
            "SELECT labels_json FROM engine_concept_labels WHERE company_key=?",
        ),
        (
            "engine_relationships",
            "SELECT payload_json FROM engine_relationships "
            "WHERE object_company_key=? ORDER BY updated_at DESC LIMIT 100",
        ),
    ):
        if table in tables:
            parts.append([tuple(r) for r in store.connection.execute(query, (company,))])
    digest = sha256(repr(parts).encode()).hexdigest()
    return (str(store.path.resolve()), db_stat.st_ino, candidate_id, VERSION, digest)


def get_score(key, now):
    if key is None:
        return None
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        stamp, expiry, value = entry
        if (
            now < stamp
            or (now - stamp).total_seconds() >= TTL_SECONDS
            or (expiry and now >= expiry)
        ):
            del _CACHE[key]
            return None
        _CACHE.move_to_end(key)
        return deepcopy(value)


def put_score(key, now, observation, score, *, boundaries=()):
    if key is None:
        return
    future_boundaries = [
        stamp.astimezone(UTC)
        for e in score.creative_capacity.signal_events
        for stamp in (e.expires_at, e.observed_at, e.effective_at, e.refresh_at)
        if isinstance(stamp, datetime) and stamp.tzinfo and stamp > now
    ]
    from ..frontier_lineage import hiring_boundaries

    future_boundaries.extend(hiring_boundaries(observation, now))
    future_boundaries.extend(stamp for stamp in boundaries if stamp > now)
    with _LOCK:
        _CACHE[key] = (now, min(future_boundaries, default=None), deepcopy((observation, score)))
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
