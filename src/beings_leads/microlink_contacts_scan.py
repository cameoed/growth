"""Standalone large-company Microlink contact hydration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import dataforseo_intelligence as seo
from . import microlink_hydration as hydration
from .async_work import run_thread_owned
from .company_research import CompanyResearchBudget
from .local_access import require_local
from .microlink_mission import EMPLOYEE_COUNT, FOOTPRINT
from .missions.reads import _selection
from .sales_engine.ranking import policy_version
from .site_research import company_host
from .store import LeadStore

logger = logging.getLogger(__name__)
REQUESTS_PER_COMPANY = hydration.PAGE_LIMIT + 1  # One same-company search plus page attempts.
RUN_TABLE = """CREATE TABLE IF NOT EXISTS microlink_contact_batches (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    mission_name TEXT NOT NULL,
    company_count INTEGER NOT NULL
)"""
SOURCE = "microlink.google"


def feature_hash(feature, *, standalone=False):
    config = {"feature": feature}
    if standalone:
        config["contact_hydration"] = True
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def recent_contact_companies(db, now, *, company_key=None):
    """Include attempts with no result and map historical candidate IDs to current aliases."""
    cutoff = (now - timedelta(days=hydration.CONTACT_REFRESH_DAYS)).isoformat()
    contact_hashes = tuple(feature_hash(feature, standalone=standalone) for feature, standalone in (
        ("contacts", False), ("contact_identity_search", False), ("contacts", True),
        ("web_search", True), ("search_result", True),
    ))
    legacy_hashes = (feature_hash("web_search"), feature_hash("search_result"))
    company_filter = (
        " AND (e.company_key=? OR m.company_key=?)" if company_key is not None else ""
    )
    rows = db.execute(
        """SELECT e.company_key,m.company_key FROM scanner_executions e
        LEFT JOIN engine_mission_members m ON m.candidate_id=e.candidate_id
        LEFT JOIN runs r ON r.id=e.run_id
        WHERE e.scanner_id=? AND e.started_at>? AND e.started_at<=?
          AND (e.config_hash IN (?,?,?,?,?) OR (
            e.config_hash IN (?,?)
            AND json_extract(r.config_json,'$.microlink_hydration.contacts')=1
          ))""" + company_filter,
        (hydration.SCANNER_ID, cutoff, now.isoformat(), *contact_hashes, *legacy_hashes,
         *((company_key, company_key) if company_key is not None else ())),
    )
    return {key for row in rows for key in row if key}


def company_contact_crawl_target(store, candidate_id, now):
    db = store.connection
    key = store._candidate_company_keys([candidate_id]).get(candidate_id)
    if not key or key in recent_contact_companies(db, now, company_key=key):
        return None
    row = db.execute(
        """SELECT c.id AS candidate_id,c.hostname,? AS company_key FROM candidates c
        JOIN snapshots s ON s.id=(SELECT MAX(id) FROM snapshots WHERE candidate_id=c.id)
        JOIN snapshot_target_intelligence t ON t.snapshot_id=s.id AND t.live=1
        WHERE c.id=? AND t.canonical_domain=?
        AND target_is_suppressed(c.source_apex,c.hostname,t.canonical_domain)=0
        AND NOT EXISTS (
            SELECT 1 FROM engine_mission_members m JOIN prospect_workflow w
            ON w.candidate_id=m.candidate_id WHERE m.company_key=t.canonical_domain
            AND (w.excluded=1 OR w.scan_enabled=0)
        )""",
        (key, candidate_id, key),
    ).fetchone()
    if row is None or CompanyResearchBudget(db, key, lane="microlink").held_until(now=now):
        return None
    return dict(row)


async def crawl_company_contacts(database, candidate_id, api_key):
    """Run the existing single-company contact lane under its normal request ceiling."""
    with LeadStore(database) as store:
        target = company_contact_crawl_target(store, candidate_id, datetime.now(UTC))
        if target is None:
            return {"status": "skipped", "requests": 0}
        run_id = store.start_run({
            "microlink_contact_hydration": True,
            "individual_company_contacts": True,
            "cohort_ids": [candidate_id],
            "max_requests": REQUESTS_PER_COMPANY,
        })

    async def checkpoint():
        return None

    result, status = {}, "failed"
    try:
        result = await hydration.run_hydration(
            database, run_id, hydration.MicrolinkHydrationOptions(
                contacts=True, web_search=True, company_limit=1,
                request_limit=REQUESTS_PER_COMPANY,
            ), api_key, [target], checkpoint=checkpoint, progress=result.update,
            contact_refresh=True, concurrency=1,
        )
        status = "partial" if result.get("failed") or result.get("limit_reached") else "complete"
    except asyncio.CancelledError:
        status = "stopped"
        raise
    except Exception:
        logger.exception("Individual Microlink contact crawl failed")
    finally:
        with LeadStore(database) as store:
            store.finish_run(run_id, status, result)
    return {**result, "status": status}


def recorded_scale(db, now):
    counts, visibility = {}, {}
    stamp = now.isoformat()
    rows = db.execute(
        """SELECT company_key,predicate,source_url,scanner_id,value_json FROM atomic_observations
        WHERE predicate IN (
            'company.employee_count','seo.domain_rank_overview','seo.search_visibility_estimate'
        ) AND validity_status='current'
          AND epistemic_status='observed' AND score_eligible=1 AND discovery_only=0
          AND first_observed_at<=? AND recorded_at<=?
          AND (effective_at IS NULL OR effective_at<=?)
          AND (expires_at IS NULL OR expires_at>?)
        ORDER BY COALESCE(effective_at,first_observed_at) DESC,recorded_at DESC,id DESC""",
        (stamp, stamp, stamp, stamp),
    )
    for row in rows:
        try:
            value = json.loads(row["value_json"])
            key = row["company_key"]
            if row["predicate"] == "company.employee_count":
                if (company_host(row["source_url"]) == key
                    and type(value) in {int, float} and 0 < value <= 10_000_000):
                    counts.setdefault(key, int(value))
            elif (row["scanner_id"] == seo.SCANNER_ID and value["target"] == key
                  and value["data_available"] is True and value["provider_estimate"] is True):
                organic = value["organic"]
                # Strong search visibility is an operating footprint proxy.
                strength = 3 if organic["count"] >= 1000 or organic["etv"] >= 10_000 else 0
                if organic["count"] >= 10_000 or organic["etv"] >= 100_000:
                    strength = 5
                visibility.setdefault(key, strength)
        except (ValueError, TypeError, KeyError):
            continue
    return counts, visibility


def size_priority(headcount, description, clue):
    if headcount is not None:
        return (2, headcount) if headcount >= 50 else (0, 0)
    for match in EMPLOYEE_COUNT.finditer(description):
        before = description[max(0, match.start() - 45):match.start()]
        company_prefix = r"\b(?:we employ|we have|our)\s+(?:(?:over|more than|about)\s+)?$"
        if re.search(company_prefix, before, re.I):
            count = int(re.sub(r"[,.]", "", match[1]))
            return (2, count) if count >= 50 else (0, 0)
    footprint = 3 if FOOTPRINT.search(description) else 0
    proxy = max(footprint, clue)
    return (1, proxy) if proxy >= 3 else (0, 0)


def saved_footprint(row):
    # Multiple schema objects can describe the same office. Require an actual
    # location index or material hiring volume before treating it as scale.
    jobs = row["open_jobs"] or 0
    locations = row["location_pages"] or 0
    return max(6 if jobs >= 50 else 4 if jobs >= 15 else 0,
               6 if locations >= 50 else 4 if locations >= 10 else 0)


def select_contacts(store, options, now):
    """Rank all size-qualified, live companies before applying the company limit."""
    db = store.connection
    try:
        mission, current, params = _selection(db, options.mission_id, now)
    except LookupError as exc:
        raise HTTPException(422, "Select an active mission in Companies") from exc
    stamp = now.isoformat()
    if options.mission_id == "beings":
        score = "r.score"
        rank_join = (
            "JOIN engine_company_ranking r INDEXED BY idx_engine_company_ranking_scores "
            "ON r.company_key=i.company_key AND r.eligible=1 "
            "AND r.excluded=0 AND r.dirty=0 AND r.policy_version=? AND r.as_of<=? "
            "AND r.valid_until>? AND r.score>0"
        )
        rank_params = [policy_version(), stamp, stamp]
    elif options.mission_id == "all":
        score, rank_join, rank_params = "0", "", []
    else:
        score = "s.score"
        rank_join = (
            "JOIN engine_mission_scores s ON s.company_key=i.company_key AND s.mission_id=? "
            f"AND s.score>0 AND {current}"
        )
        rank_params = [options.mission_id, *params]
    seen = set() if options.force_contacts else recent_contact_companies(db, now)
    counts, visibility = recorded_scale(db, now)
    candidates = []
    rows = db.execute(
        f"""WITH live AS MATERIALIZED (
            SELECT c.id,c.hostname,c.source_apex,i.company_key,i.snapshot_id,{score} score,
                   COALESCE(edge.discovery_rank,0) clue
            FROM engine_mission_inventory i
            JOIN candidates c ON c.id=i.candidate_id
            JOIN snapshot_target_intelligence ti ON ti.snapshot_id=i.snapshot_id AND ti.live=1
            {rank_join}
            LEFT JOIN candidate_source_edges edge ON edge.candidate_id=c.id AND edge.source_apex=?
            WHERE NOT EXISTS (
                SELECT 1 FROM engine_mission_members member JOIN prospect_workflow w
                ON w.candidate_id=member.candidate_id WHERE member.company_key=i.company_key
                AND (w.excluded=1 OR w.scan_enabled=0)
            ) ORDER BY i.snapshot_id
        ) SELECT live.*,
                 json_extract(o.observation_json,'$.description') description,
                 json_extract(o.observation_json,'$.web.open_jobs') open_jobs,
                 json_extract(o.observation_json,'$.web.sitemap_counts.locations') location_pages
          FROM live CROSS JOIN snapshots o ON o.id=live.snapshot_id""",
        (*rank_params, SOURCE),
    )
    for row in rows:
        key = row["company_key"]
        if key in seen:
            continue
        clue = max(row["clue"], saved_footprint(row), visibility.get(key, 0))
        tier, size = size_priority(counts.get(key), row["description"] or "", clue)
        if tier:
            candidates.append((tier, size, row))
    candidates.sort(key=lambda x: (-x[0], -x[1], -x[2]["score"], x[2]["company_key"]))
    selected = []
    for tier, _, row in candidates:
        if db.execute("SELECT target_is_suppressed(?,?,?)", (
            row["source_apex"], row["hostname"], row["company_key"],
        )).fetchone()[0]:
            continue
        if CompanyResearchBudget(db, row["company_key"], lane="microlink").held_until(now=now):
            continue
        selected.append({
            "candidate_id": row["id"], "hostname": row["hostname"],
            "company_key": row["company_key"], "size_evidence": tier == 2,
        })
        if len(selected) == options.company_count:
            break
    return mission, selected


class ContactOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_count: int = Field(default=25, ge=1, le=5000)
    mission_id: str = Field(default="beings", max_length=100)
    force_contacts: bool = False


class ContactStart(ContactOptions):
    estimate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed_max_requests: int = Field(ge=1, le=45000)


class MicrolinkContactScan:
    def __init__(self, database, api_key, blocked):
        self.database, self.api_key, self.blocked = database, api_key, blocked
        self.lock = asyncio.Lock()
        self.task = None
        self.run_id = None
        self.stop_requested = False
        self.progress = {}

    @property
    def active(self):
        return self.lock.locked() or bool(self.task and not self.task.done())

    def selection(self, options):
        with LeadStore(self.database(), initialize=False, read_only=True) as store:
            store.connection.execute("BEGIN")
            mission, cohort = select_contacts(store, options, datetime.now(UTC))
        quote = [options.mission_id, mission["revision"], options.company_count,
                 options.force_contacts, cohort]
        return {
            "mission": mission["name"], "companies": len(cohort),
            "headcount_supported": sum(row["size_evidence"] for row in cohort),
            "max_requests": REQUESTS_PER_COMPANY * len(cohort),
            "configured": bool(self.api_key()),
            "force_contacts": options.force_contacts,
            "estimate_id": hashlib.sha256(json.dumps(quote, sort_keys=True).encode()).hexdigest(),
        }, cohort

    @staticmethod
    def register(database, quote, cohort, mission_id):
        with LeadStore(database, initialize=False) as store:
            with store.connection:
                store.connection.execute(RUN_TABLE)
                store.connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scanner_executions_run_scanner "
                    "ON scanner_executions(run_id,scanner_id) WHERE run_id IS NOT NULL"
                )
            run_id = store.start_run({
                "microlink_contact_hydration": True, "mission_id": mission_id,
                "cohort_ids": [row["candidate_id"] for row in cohort],
                "max_requests": quote["max_requests"],
                "force_contacts": quote.get("force_contacts", False),
            })
            with store.connection:
                store.connection.execute(
                    "INSERT INTO microlink_contact_batches VALUES (?,?,?)",
                    (run_id, quote["mission"], quote["companies"]),
                )
            return run_id

    @staticmethod
    def finish(database, run_id, status, result):
        with LeadStore(database, initialize=False) as store:
            store.finish_run(run_id, status, result)

    async def work(self, database, run_id, quote, key, cohort):
        result, status = {}, "failed"

        async def checkpoint():
            if self.stop_requested:
                raise asyncio.CancelledError

        try:
            options = hydration.MicrolinkHydrationOptions(
                contacts=True, web_search=True, company_limit=len(cohort),
                request_limit=quote["max_requests"],
            )
            result = await hydration.run_hydration(
                database, run_id, options, key, cohort, checkpoint=checkpoint,
                progress=self.progress.update, contact_refresh=True, concurrency=12,
                force_contacts=quote.get("force_contacts", False),
            )
            incomplete = result.get("failed") or result.get("limit_reached")
            status = "partial" if incomplete else "complete"
        except asyncio.CancelledError:
            result, status = dict(self.progress), "stopped"
        except Exception:
            result = dict(self.progress)
            logger.exception("Microlink contact hydration failed")
        finally:
            await run_thread_owned(self.finish, database, run_id, status, result)

    async def start(self, options):
        if self.active:
            raise HTTPException(409, "Microlink contacts is already running")
        async with self.lock:
            if self.blocked():
                raise HTTPException(409, "Wait for the active scan to finish")
            key = self.api_key()
            if not key:
                raise HTTPException(409, "Configure Microlink in Settings")
            quote, cohort = await run_thread_owned(self.selection, options)
            if (not cohort or quote["estimate_id"] != options.estimate_id
                    or quote["max_requests"] != options.confirmed_max_requests):
                raise HTTPException(409, "Get a fresh estimate for the due companies")
            database = self.database()
            run_id = await run_thread_owned(
                self.register, database, quote, cohort, options.mission_id,
            )
            self.run_id, self.stop_requested, self.progress = run_id, False, {}
            self.task = asyncio.create_task(self.work(database, run_id, quote, key, cohort))
            return {"status": "running", **quote, "requests": 0, "completed": 0, "failed": 0}

    def status(self):
        with LeadStore(self.database(), initialize=False, read_only=True) as store:
            db = store.connection
            db.execute("BEGIN")
            if not db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='microlink_contact_batches'"
            ).fetchone():
                return {"status": "idle"}
            row = db.execute(
                "SELECT b.*,r.status,r.summary_json,r.started_at,r.finished_at "
                "FROM microlink_contact_batches b "
                "JOIN runs r ON r.id=b.run_id ORDER BY b.run_id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"status": "idle"}
            attempts = db.execute(
                "SELECT COUNT(*) FROM scanner_executions WHERE scanner_id=? AND run_id=?",
                (hydration.SCANNER_ID, row["run_id"]),
            ).fetchone()[0]
            receipt_scope = (hydration.SCANNER_ID, row["run_id"])
            failed_sql = "status NOT IN ('running','success','success_empty','not_applicable')"
            error_total = db.execute(
                "SELECT COUNT(*) FROM scanner_executions WHERE scanner_id=? AND run_id=? AND "
                + failed_sql, receipt_scope,
            ).fetchone()[0]
            log = {}
            for field, filter_sql in (("events", "1"), ("errors", failed_sql)):
                receipts = db.execute(
                    f"SELECT company_key,status,started_at,{failed_sql} AS failed "
                    "FROM scanner_executions WHERE scanner_id=? AND run_id=? AND "
                    f"{filter_sql} ORDER BY id DESC LIMIT 40", receipt_scope,
                ).fetchall()
                log[field] = [
                    {
                        "timestamp": receipt["started_at"],
                        "level": "error" if receipt["failed"] else "info",
                        "message": receipt["company_key"],
                        "detail": receipt["status"].replace("_", " "),
                    }
                    for receipt in reversed(receipts)
                ]
        result = json.loads(row["summary_json"] or "{}")
        status = row["status"]
        if status == "running":
            if self.run_id == row["run_id"] and self.task and not self.task.done():
                status = "stopping" if self.stop_requested else "running"
                result = dict(self.progress)
            else:
                status = "interrupted"
        return {
            "status": status, "mission": row["mission_name"], "companies": row["company_count"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "error_total": error_total, **log,
            "max_requests": REQUESTS_PER_COMPANY * row["company_count"], "requests": attempts,
            "completed": result.get("completed", 0), "failed": result.get("failed", 0),
        }

    async def shutdown(self):
        if self.task and not self.task.done():
            self.stop_requested = True
            with suppress(Exception):
                await asyncio.shield(self.task)

    def router(self):
        router = APIRouter(prefix="/api/microlink-contacts", dependencies=[Depends(require_local)])

        @router.post("/estimate")
        def estimate(options: ContactOptions):
            return self.selection(options)[0]

        @router.post("/runs", status_code=202)
        async def start(options: ContactStart):
            return await self.start(options)

        @router.get("/status")
        def status():
            return self.status()

        @router.post("/stop", status_code=202)
        async def stop():
            if self.task and not self.task.done():
                self.stop_requested = True
                return {"status": "stopping"}
            return await run_thread_owned(self.status)

        return router
