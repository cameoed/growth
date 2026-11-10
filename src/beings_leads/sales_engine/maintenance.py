"""Local evidence refresh and empirical research yield. Never fetches a provider."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from .persistence import SalesEngineRepository

VERSION = "sales-engine-projection-v2-hydrated"
REVIEW_WINDOW_DAYS = 90
CLOUD_MISSION_BATCH_SIZE = 8
CLOUD_SHARED_BATCH_SIZE = 16
logger = logging.getLogger(__name__)
mission_write_lock = Lock()
_ranking_stage: tuple[str, str | None, tuple[int, int] | None] = ("waiting", None, None)


def _set_ranking_stage(stage: str, mission_id: str | None = None, *, shared=None) -> None:
    global _ranking_stage
    _ranking_stage = (stage, mission_id, shared)


def ranking_worker_state(connection, mission_id: str) -> dict:
    stage, active_mission, shared = _ranking_stage
    payload = {"ranking_worker_status": stage}
    if active_mission is not None and active_mission != mission_id:
        payload["ranking_worker_status"] = "other_mission"
        row = connection.execute(
            "SELECT name FROM engine_missions WHERE id=? AND archived=0", (active_mission,),
        ).fetchone()
        if row is not None:
            payload["ranking_worker_mission_name"] = row[0]
    if shared is not None:
        payload["shared_ranking_progress"] = {"checked": shared[0], "total": shared[1]}
    return payload


def _shared_ranking_progress(checked: int, total: int) -> None:
    _set_ranking_stage("shared_rankings", shared=(checked, total))


def refresh_projections(store, *, limit: int = 100, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("refresh time requires a timezone")
    now = now.astimezone(UTC)
    repository = SalesEngineRepository(store.connection)
    # Savepoints preserve an enclosing operator transaction; independent batches commit
    # each result separately so one malformed historical row cannot undo good refreshes.
    with repository._transaction():
        store.connection.execute("""CREATE TABLE IF NOT EXISTS engine_projection_refresh(
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id),
            snapshot_id INTEGER NOT NULL,
            version TEXT NOT NULL, refreshed_at TEXT NOT NULL)""")
        store.connection.execute("""CREATE TABLE IF NOT EXISTS engine_projection_failures(
            candidate_id INTEGER NOT NULL REFERENCES candidates(id), snapshot_id INTEGER NOT NULL,
            version TEXT NOT NULL, failed_at TEXT NOT NULL, retry_after TEXT NOT NULL,
            failure_count INTEGER NOT NULL CHECK(failure_count>0), error_kind TEXT NOT NULL,
            PRIMARY KEY(candidate_id,snapshot_id,version))""")
    rows = store.connection.execute(
        """
        SELECT ti.candidate_id, ti.snapshot_id FROM snapshot_target_intelligence ti
        LEFT JOIN engine_projection_refresh r ON r.candidate_id=ti.candidate_id
        LEFT JOIN engine_projection_failures f ON f.candidate_id=ti.candidate_id
            AND f.snapshot_id=ti.snapshot_id AND f.version=?
        LEFT JOIN prospect_workflow w ON w.candidate_id=ti.candidate_id
        WHERE ti.live=1 AND COALESCE(w.excluded,0)=0
          AND ti.snapshot_id=(SELECT MAX(s.id) FROM snapshots s
                             WHERE s.candidate_id=ti.candidate_id)
          AND (r.candidate_id IS NULL OR r.version<>? OR r.snapshot_id<>ti.snapshot_id
               OR r.refreshed_at<?)
          AND (f.candidate_id IS NULL OR f.retry_after<=?)
        ORDER BY ti.disqualified, ti.founder_priority_score DESC, ti.candidate_id
        LIMIT ?
    """,
        (
            VERSION, VERSION, (now - timedelta(days=1)).isoformat(), now.isoformat(),
            max(1, min(limit, 500)),
        ),
    ).fetchall()
    completed = 0
    errors = []
    for row in rows:
        try:
            with repository._transaction():
                if not store.reproject_candidate_ad_intelligence(
                    row["candidate_id"], reference_time=now, commit=False,
                ):
                    raise ValueError("latest observation is unavailable for projection")
                store.connection.execute(
                    """INSERT INTO engine_projection_refresh VALUES(?,?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,
                    version=excluded.version, refreshed_at=excluded.refreshed_at""",
                    (row["candidate_id"], row["snapshot_id"], VERSION, now.isoformat()),
                )
                store.connection.execute(
                    "DELETE FROM engine_projection_failures WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
            completed += 1
        except Exception as exc:
            with repository._transaction():
                prior = store.connection.execute(
                    """SELECT failure_count FROM engine_projection_failures
                    WHERE candidate_id=? AND snapshot_id=? AND version=?""",
                    (row["candidate_id"], row["snapshot_id"], VERSION),
                ).fetchone()
                failures = (prior["failure_count"] if prior else 0) + 1
                retry = now + timedelta(hours=min(24, 2 ** min(failures - 1, 5)))
                store.connection.execute(
                    """INSERT INTO engine_projection_failures VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(candidate_id,snapshot_id,version) DO UPDATE SET
                    failed_at=excluded.failed_at,retry_after=excluded.retry_after,
                    failure_count=excluded.failure_count,error_kind=excluded.error_kind""",
                    (
                        row["candidate_id"], row["snapshot_id"], VERSION, now.isoformat(),
                        retry.isoformat(), failures, type(exc).__name__,
                    ),
                )
            errors.append({"candidate_id": row["candidate_id"], "error": type(exc).__name__})
            logger.exception("local projection refresh failed candidate=%s", row["candidate_id"])
    return {
        "refreshed": completed,
        "selected": len(rows),
        "as_of": now.isoformat(),
        "version": VERSION,
        "errors": errors,
        "provider_requests": 0,
    }


async def maintain_compiler_receipts(path) -> None:
    """Preserve recovery of already-paid interpretations with desktop-owned scoring."""
    from ..missions.compiler import reconcile_finished
    from ..store import LeadStore

    def reconcile():
        if not mission_write_lock.acquire(blocking=False):
            return
        try:
            with LeadStore(path, initialize=False) as store:
                reconcile_finished(store.connection)
        finally:
            mission_write_lock.release()

    while True:
        try:
            await asyncio.to_thread(reconcile)
        except Exception:
            logger.exception("Mission compiler receipt reconciliation failed")
        await asyncio.sleep(15)


async def maintain_projections(database_path, *, should_defer=None) -> None:
    from ..missions.compute import MissionComputePool
    from ..missions.scoring import BACKGROUND_BATCH_SIZE, LARGE_BACKGROUND_BATCH_SIZE
    from ..oracle_deployment import REMOTE_ROOT, ROOT
    from ..scan_resources import LIMITS
    from ..store import LeadStore

    def deferral_reason():
        if mission_write_lock.locked():
            return "mission_setup"
        if should_defer is not None and should_defer():
            return "smart_scan"
        return None

    def defer():
        return deferral_reason() is not None

    local_background = Path(REMOTE_ROOT) != ROOT
    pool = MissionComputePool(
        workers=None if local_background else 2,
        reserve_cpu=False, should_defer=defer, local_background=local_background,
    )
    # Give each mission and the shared ranking frequent turns on Oracle.
    if not local_background:
        mission_batch_size = CLOUD_MISSION_BATCH_SIZE
    elif pool.workers > LIMITS["cpu"][1]:
        mission_batch_size = LARGE_BACKGROUND_BATCH_SIZE
    else:
        mission_batch_size = BACKGROUND_BATCH_SIZE

    def batch():
        if defer():
            return None
        _set_ranking_stage("preparing")
        with LeadStore(database_path()) as store:
            from ..missions.compiler import reconcile_finished
            from ..missions.scoring import refresh as refresh_missions
            from .ranking import refresh_rankings
            from .ranking_modes import refresh_lenses

            def rank_missions():
                started = time.monotonic()
                _set_ranking_stage("selecting")
                result = refresh_missions(
                    store, limit=mission_batch_size, compute_pool=pool,
                    on_stage=_set_ranking_stage, coalesce=not local_background,
                )
                return result, time.monotonic() - started

            reconcile_finished(store.connection)
            if not pool.accepting:
                return None
            if not local_background:
                missions, mission_seconds = rank_missions()
                if not pool.accepting:
                    return None
            shared_limit = 100 if local_background else CLOUD_SHARED_BATCH_SIZE
            _set_ranking_stage("shared_rankings")
            result = refresh_rankings(
                store, limit=shared_limit, prioritize_expired_scores=True,
                should_defer=lambda: not pool.accepting, on_progress=_shared_ranking_progress,
            )
            if not pool.accepting:
                return None
            _set_ranking_stage("ranking_views")
            lenses = refresh_lenses(store)
            if not pool.accepting:
                return None
            if local_background:
                missions, mission_seconds = rank_missions()
            result["mission_seconds"] = mission_seconds
            result["mission_refreshed"] = missions["refreshed"]
            result["mission_errors"] = missions["errors"]
            result["busy"] = (result["selected"] == shared_limit or lenses["refreshed"]
                              or missions["selected"] >= mission_batch_size)
            return result

    last_report = time.monotonic()
    completed, compute_seconds = 0, 0
    try:
        _set_ranking_stage("waiting")
        await asyncio.sleep(10)
        while True:
            reason = deferral_reason()
            if reason:
                _set_ranking_stage(reason)
                await asyncio.sleep(1)
                continue
            try:
                task = asyncio.create_task(asyncio.to_thread(batch))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # to_thread cannot cancel a running publisher. Finish its bounded
                    # batch before closing workers or returning from app shutdown.
                    pool.request_stop()
                    await task
                    raise
                if result is None:
                    _set_ranking_stage(deferral_reason() or "waiting")
                    await asyncio.sleep(1)
                    continue
                completed += result["mission_refreshed"]
                compute_seconds += result["mission_seconds"]
                if completed and time.monotonic() - last_report >= 30:
                    logger.info("local mission scoring refreshed=%s seconds=%.3f workers=%s",
                                completed, compute_seconds, pool.workers)
                    completed, compute_seconds = 0, 0
                    last_report = time.monotonic()
                delay = 0.25 if result["busy"] else 15
                _set_ranking_stage("waiting")
            except Exception:
                _set_ranking_stage("retrying")
                logger.exception("sales engine local refresh failed")
                # A dead process pool is recreated lazily on the next resumable batch.
                await asyncio.to_thread(pool.close)
                delay = 60
            await asyncio.sleep(delay)
    finally:
        _set_ranking_stage("waiting")
        await asyncio.to_thread(pool.close)


def empirical_scanner_yield(store, *, now: datetime | None = None) -> dict:
    """Shrink observed evidence yield toward priors; emptiness is not negative buyer intent.

    At most 20k recent completed executions contribute. Cache on the store avoids repeated
    aggregation in a scan batch. Human utility uses each company's latest explicit answer
    per action and field in the last 90 days. Repeated reviews cannot manufacture an
    independent sample. These are research reliability estimates, not conversion odds.
    """
    cache_key = now.isoformat() if now is not None else None
    cached = getattr(store, "_engine_yield_cache", None)
    if (
        cached and len(cached) == 3
        and time.monotonic() - cached[0] < 300 and cached[2] == cache_key
    ):
        return cached[1]
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("research yield time requires a timezone")
    now = now.astimezone(UTC)
    rows = store.connection.execute("""SELECT scanner_id, COUNT(*) AS n,
        SUM(status IN ('success','partial') AND observation_count>0) AS useful,
        SUM(status IN ('success','success_empty','partial')) AS retrieved,
        SUM(status='robots_blocked') AS blocked, AVG(request_count) AS requests,
        AVG(cost_microusd) AS paid_cost
        FROM (SELECT * FROM scanner_executions WHERE status NOT IN ('running','not_applicable')
              ORDER BY id DESC LIMIT 20000) GROUP BY scanner_id""").fetchall()
    result = {row["scanner_id"]: dict(row) for row in rows}
    if store.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_research_reviews'"
    ).fetchone():
        reviews = store.connection.execute("""WITH recent AS (
            SELECT id,company_key,recorded_at,
                json_extract(payload_json,'$.research_action') AS scanner_id,
                CASE WHEN json_type(payload_json,'$.decision_changed') IN ('true','false')
                    THEN json_extract(payload_json,'$.decision_changed') END AS changed,
                CASE WHEN json_type(payload_json,'$.useful') IN ('true','false')
                    THEN json_extract(payload_json,'$.useful') END AS useful,
                json_extract(payload_json,'$.actual_minutes') AS minutes
            FROM engine_research_reviews
            WHERE recorded_at>=? AND recorded_at<=? AND observed_at>=? AND observed_at<=?
        ), ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY company_key,scanner_id
                    ORDER BY changed IS NOT NULL DESC,recorded_at DESC,id DESC) AS decision_rank,
                ROW_NUMBER() OVER (PARTITION BY company_key,scanner_id
                    ORDER BY useful IS NOT NULL DESC,recorded_at DESC,id DESC) AS useful_rank,
                ROW_NUMBER() OVER (PARTITION BY company_key,scanner_id
                    ORDER BY recorded_at DESC,id DESC) AS review_rank,
                AVG(minutes) OVER (PARTITION BY company_key,scanner_id) AS company_minutes
            FROM recent
        ) SELECT scanner_id,COUNT(DISTINCT company_key) AS reviews,COUNT(*) AS review_events,
            COUNT(CASE WHEN decision_rank=1 THEN changed END) AS labelled_decisions,
            SUM(CASE WHEN decision_rank=1 THEN COALESCE(changed,0) ELSE 0 END) AS changed_decisions,
            COUNT(CASE WHEN useful_rank=1 THEN useful END) AS labelled_useful,
            SUM(CASE WHEN useful_rank=1 THEN COALESCE(useful,0) ELSE 0 END) AS human_useful,
            AVG(CASE WHEN review_rank=1 THEN company_minutes END) AS review_minutes
        FROM ranked GROUP BY scanner_id""", (
            (now - timedelta(days=REVIEW_WINDOW_DAYS)).isoformat(), now.isoformat(),
            (now - timedelta(days=REVIEW_WINDOW_DAYS)).isoformat(), now.isoformat(),
        )).fetchall()
        for row in reviews:
            evidence = result.setdefault(row["scanner_id"], {
                "scanner_id": row["scanner_id"], "n": 0, "useful": 0, "retrieved": 0,
                "blocked": 0, "requests": 0, "paid_cost": 0,
            })
            evidence.update(dict(row))
            evidence["review_window_days"] = REVIEW_WINDOW_DAYS
            evidence["human_sample_basis"] = "latest_explicit_label_per_company_and_action"
    store._engine_yield_cache = (time.monotonic(), result, cache_key)
    return result


def adjusted_research_prior(prior: float, evidence: dict | None) -> float:
    if not evidence:
        return prior
    # Ten prior observations retain exploration for new sources; a floor prevents lockout.
    return max(
        0.03,
        min(
            0.97,
            (10 * prior + evidence.get("retrieved", evidence.get("useful", 0)))
            / (10 + evidence["n"]),
        ),
    )


def research_utility(evidence: dict | None, *, prior: float, cost: float) -> tuple[float, float]:
    """Use decision changes or usefulness after five labelled companies; charge review time.

    Successful empty evidence can still change a decision. Provider charges, request overhead,
    and human review time are cost penalties, not negative customer labels.
    """
    if not evidence:
        return prior, cost
    if evidence.get("labelled_decisions", 0) >= 5:
        utility = (5 * prior + evidence["changed_decisions"]) / (5 + evidence["labelled_decisions"])
    elif evidence.get("labelled_useful", 0) >= 5:
        utility = (5 * prior + evidence["human_useful"]) / (5 + evidence["labelled_useful"])
    else:
        utility = prior
    penalty = cost + min(0.15, (evidence.get("review_minutes") or 0) / 120)
    penalty += min(0.15, (evidence.get("paid_cost") or 0) / 10_000_000)
    penalty += min(0.10, (evidence.get("requests") or 0) / 200)
    return max(0.03, min(0.97, utility)), min(0.5, penalty)
