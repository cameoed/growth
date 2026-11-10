"""Private Scan-menu access to the existing Microlink discovery ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from itertools import zip_longest
from threading import Event
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import microlink_discovery as discovery
from . import microlink_mission as mission_search
from .local_access import require_local
from .missions.store import get_mission
from .store import LeadStore

logger = logging.getLogger(__name__)
RUN_TABLE = """CREATE TABLE IF NOT EXISTS microlink_discovery_scan_runs (
    budget_id TEXT PRIMARY KEY REFERENCES microlink_discovery_budgets(budget_id),
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_limit INTEGER NOT NULL
)"""


PLAN_TABLE = """CREATE TABLE IF NOT EXISTS microlink_discovery_scan_plans (
    budget_id TEXT PRIMARY KEY REFERENCES microlink_discovery_scan_runs(budget_id),
    plan_json TEXT NOT NULL,
    mission_name TEXT NOT NULL,
    markets_json TEXT NOT NULL
)"""
TERMINAL_RESUMABLE = {"interrupted", "stopped", "partial", "failed"}


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lane: Literal["both", "hiring", "launch", "mission"] = "both"
    market: str = Field(default="all", pattern=r"^(all|[a-z]{2})$")
    mission_id: str = Field(default="beings", max_length=100)
    request_limit: int = Field(default=24, ge=1, le=5000)
    candidate_limit: int = Field(default=100, ge=1, le=5000)
    prefer_large: bool = True
    limit_to_com: bool = False
    limit_to_ai: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        if self.limit_to_com and self.limit_to_ai:
            raise ValueError("Choose one domain restriction")
        if self.market != "all" and self.market not in mission_search.LOCATIONS:
            raise ValueError("Unsupported discovery market")
        if self.lane != "mission" and (
            self.request_limit > 160 or self.candidate_limit > 500
        ):
            raise ValueError("Creative signals use core Western markets and the original limits")
        return self


class SearchStart(SearchOptions):
    confirmed_max_requests: int = Field(ge=1, le=5000)
    estimate_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class MicrolinkScan:
    def __init__(self, database, api_key, blocked):
        self.database, self.api_key, self.blocked = database, api_key, blocked
        self.launch_lock = asyncio.Lock()
        self.task = None
        self.budget = None
        self.stop_requested = Event()

    @property
    def active(self):
        return self.launch_lock.locked() or bool(self.task and not self.task.done())

    def selection(self, options, anchor):
        mission_name = ""
        mission_revision = 0
        with LeadStore(self.database(), initialize=False, read_only=True) as store:
            db = store.connection
            if options.lane == "mission":
                try:
                    mission = get_mission(db, options.mission_id)
                except LookupError as exc:
                    raise HTTPException(422, "Select an active mission in Companies") from exc
                mission_name = mission["name"]
                mission_revision = mission["revision"]
                if not mission_search.mission_terms(options.mission_id, mission["spec"]):
                    raise HTTPException(422, "Save a mission with a company activity first")
                recipes = mission_search.plan(
                    options.mission_id, mission["spec"], anchor, options.market,
                    prefer_large=options.prefer_large,
                )
            else:
                markets = (
                    tuple(mission_search.LOCATIONS) if options.market == "all"
                    else (options.market,)
                )
                groups = [(
                    r for r in discovery.recipes(anchor, market)
                    if options.lane == "both" or r[0] == options.lane
                ) for market in markets]
                recipes = (r for group in zip_longest(*groups) for r in group if r is not None)
            seen = set()
            if db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='microlink_discovery_requests'"
            ).fetchone():
                seen = {row[0] for row in db.execute(
                    "SELECT query_key FROM microlink_discovery_requests"
                )}
        selected = []
        domain_filter = "site:.ai" if options.limit_to_ai else ""
        if options.limit_to_com:
            domain_filter = "site:.com"
        for recipe in recipes:
            if domain_filter:
                recipe = (*recipe[:4], f"{recipe[4]} {domain_filter}", *recipe[5:])
            key = discovery.query_key(recipe)
            if key in seen:
                continue
            seen.add(key)
            selected.append(recipe)
            if len(selected) == options.request_limit:
                break
        quote_input = [options.model_dump(include=set(SearchOptions.model_fields)),
                       mission_revision, mission_search.POLICY, selected]
        quote = {
            "max_requests": len(selected),
            "markets": list(dict.fromkeys(recipe[2] for recipe in selected)),
            "mission": mission_name,
            "mission_revision": mission_revision,
            "configured": bool(self.api_key()),
            "estimate_id": hashlib.sha256(
                json.dumps(quote_input, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }
        return quote, selected

    def estimate(self, options, anchor):
        return self.selection(options, anchor)[0]

    def register(self, database, budget, cap, candidate_limit, plan=None):
        with LeadStore(database, initialize=False) as store:
            db = store.connection
            discovery.initialize(db, budget, cap)
            with db:
                db.execute(RUN_TABLE)
                db.execute(PLAN_TABLE)
                db.execute(
                    "INSERT INTO microlink_discovery_scan_runs VALUES (?,?,'running',?)",
                    (budget, datetime.now(UTC).isoformat(), candidate_limit),
                )
                if plan is not None:
                    db.execute(
                        "INSERT INTO microlink_discovery_scan_plans VALUES (?,?,?,?)",
                        (budget, json.dumps(plan, default=str), plan["mission"],
                         json.dumps(plan["markets"])),
                    )

    def launch(self, database, budget, cap, candidate_limit, plan, key, status):
        args = SimpleNamespace(
            database=database, budget=budget, cap=cap, wave=cap,
            date=datetime.fromisoformat(plan["anchor"]).date(), lane=plan["lane"],
            market=plan["market"], max_candidates=candidate_limit, plan=plan["recipes"],
            prefer_large=plan["prefer_large"],
        )
        self.budget = budget
        self.stop_requested.clear()
        self.task = asyncio.create_task(asyncio.to_thread(self.work, args, key))
        self.task.add_done_callback(self.log_worker_failure)
        return {**status, "status": "running", "resumable": False}

    def work(self, args, key):
        status = "failed"
        with LeadStore(args.database, initialize=False) as store:
            try:
                counts = asyncio.run(discovery.run_search(
                    store, args, key, stopped=self.stop_requested.is_set,
                ))
                incomplete = store.connection.execute(
                    "SELECT 1 FROM microlink_discovery_requests "
                    "WHERE budget_id=? AND state<>'complete' LIMIT 1", (args.budget,),
                ).fetchone()
                status = (
                    "stopped" if self.stop_requested.is_set() else
                    "partial" if incomplete or "database_busy" in counts else "complete"
                )
            except Exception:
                logger.exception("Microlink discovery scan failed")
            finally:
                with store.connection:
                    store.connection.execute(
                        "UPDATE microlink_discovery_scan_runs SET status=? WHERE budget_id=?",
                        (status, args.budget),
                    )

    async def start(self, options):
        if self.active:
            raise HTTPException(409, "Microlink discovery is already running")
        async with self.launch_lock:
            if self.blocked():
                raise HTTPException(409, "Wait for the active scan to finish")
            key = self.api_key()
            if not key:
                raise HTTPException(409, "Configure Microlink in Settings")
            anchor = datetime.now(UTC).date()
            estimate, recipes = await asyncio.to_thread(self.selection, options, anchor)
            cap = estimate["max_requests"]
            if not cap or cap != options.confirmed_max_requests or (
                estimate["estimate_id"] != options.estimate_id
            ):
                raise HTTPException(409, "Get a new estimate for the available queries")
            database = self.database()
            budget = f"scan-{uuid.uuid4().hex}"
            plan = {"recipes": recipes, "anchor": anchor.isoformat(), "lane": options.lane,
                    "market": options.market, "prefer_large": options.prefer_large,
                    "mission_search_policy": mission_search.POLICY,
                    "mission": estimate["mission"], "mission_id": options.mission_id,
                    "mission_revision": estimate["mission_revision"],
                    "markets": estimate["markets"]}
            await asyncio.to_thread(
                self.register, database, budget, cap, options.candidate_limit, plan,
            )
            return self.launch(database, budget, cap, options.candidate_limit, plan, key, {
                "max_requests": cap, "requests": 0, "candidates": 0,
                "mission": plan["mission"], "markets": plan["markets"],
            })

    def resume_plan(self, database):
        with LeadStore(database, initialize=False) as store:
            db = store.connection
            row = self.latest(db)
            if not row:
                raise HTTPException(409, "No plan to resume")
            status = self.run_status(db, row)
            if not status.get("resumable"):
                raise HTTPException(409, "This plan has no remaining approved work")
            with db:
                db.execute("UPDATE microlink_discovery_scan_runs SET status='running' "
                           "WHERE budget_id=?", (row["budget_id"],))
            plan = json.loads(db.execute(
                "SELECT plan_json FROM microlink_discovery_scan_plans WHERE budget_id=?",
                (row["budget_id"],),
            ).fetchone()[0])
            return row["budget_id"], row["request_cap"], row["candidate_limit"], plan, status

    async def resume(self):
        if self.active:
            raise HTTPException(409, "Microlink discovery is already running")
        async with self.launch_lock:
            if self.blocked():
                raise HTTPException(409, "Wait for the active scan to finish")
            key = self.api_key()
            if not key:
                raise HTTPException(409, "Configure Microlink in Settings")
            database = self.database()
            budget, cap, candidate_limit, plan, status = await asyncio.to_thread(
                self.resume_plan, database,
            )
            return self.launch(database, budget, cap, candidate_limit, plan, key, status)

    @staticmethod
    def log_worker_failure(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("Microlink discovery worker could not finish its receipt",
                         exc_info=task.exception())

    @staticmethod
    def latest(db):
        if not db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='microlink_discovery_scan_runs'"
        ).fetchone():
            return None
        return db.execute(
            """SELECT r.budget_id,r.status,r.started_at,r.candidate_limit,b.request_cap,
                (SELECT COUNT(*) FROM microlink_discovery_requests q
                 WHERE q.budget_id=r.budget_id) requests,
                (SELECT COUNT(DISTINCT l.candidate_id) FROM microlink_discovery_leads l
                 JOIN microlink_discovery_requests q USING(query_key)
                 WHERE q.budget_id=r.budget_id) candidates
            FROM microlink_discovery_scan_runs r JOIN microlink_discovery_budgets b
            USING(budget_id) ORDER BY r.started_at DESC LIMIT 1"""
        ).fetchone()

    def run_status(self, db, row):
        status = row["status"]
        if status == "running":
            if self.budget != row["budget_id"] or not self.task or self.task.done():
                status = "interrupted"
            elif self.stop_requested.is_set():
                status = "stopping"
        summary = None
        if db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='microlink_discovery_scan_plans'"
        ).fetchone():
            summary = db.execute(
                "SELECT mission_name,markets_json,plan_json FROM microlink_discovery_scan_plans "
                "WHERE budget_id=?", (row["budget_id"],),
            ).fetchone()
        payload = {"status": status, "max_requests": row["request_cap"],
                   "requests": row["requests"], "candidates": row["candidates"]}
        counts = dict(map(tuple, db.execute(
            "SELECT state,COUNT(*) FROM microlink_discovery_requests WHERE budget_id=? "
            "GROUP BY state", (row["budget_id"],),
        )))
        payload.update(
            started_at=row["started_at"],
            completed_requests=counts.get("complete", 0) + counts.get("failed", 0),
            error_total=counts.get("failed", 0),
        )
        plan = json.loads(summary["plan_json"]) if summary else {}
        recipes = plan.get("recipes", [])
        queries = {discovery.query_key(recipe): recipe[4] for recipe in recipes}
        for field, failures_only in (("events", False), ("errors", True)):
            receipts = db.execute(
                "SELECT query_key,state,recorded_at,result_count FROM microlink_discovery_requests "
                "WHERE budget_id=? AND (?=0 OR state='failed') "
                "ORDER BY recorded_at DESC,query_key LIMIT 40",
                (row["budget_id"], int(failures_only)),
            ).fetchall()
            payload[field] = [
                {
                    "timestamp": receipt["recorded_at"],
                    "query": queries.get(receipt["query_key"], ""),
                    "level": {"complete": "success", "failed": "error", "reserved": "info"}[
                        receipt["state"]
                    ],
                    "message": {
                        "complete": f"Search returned {receipt['result_count']} results",
                        "failed": "Microlink search failed",
                        "reserved": "Search request reserved",
                    }[receipt["state"]],
                }
                for receipt in reversed(receipts)
            ]
        if summary:
            policy = plan.get("mission_search_policy")
            current_scope = all(
                recipe[2] in mission_search.LOCATIONS
                and (recipe[0] != "mission" or policy == mission_search.POLICY)
                for recipe in recipes
            )
            payload.update(mission=summary["mission_name"],
                           markets=json.loads(summary["markets_json"]), resumable=(
                status in TERMINAL_RESUMABLE and row["requests"] < row["request_cap"]
                and bool(recipes) and current_scope
                and row["candidates"] < row["candidate_limit"]
            ))
        return payload

    def status(self):
        with LeadStore(self.database(), initialize=False, read_only=True) as store:
            db = store.connection
            db.execute("BEGIN")
            row = self.latest(db)
            return self.run_status(db, row) if row else {"status": "idle"}

    async def shutdown(self):
        if self.task and not self.task.done():
            self.stop_requested.set()
            with suppress(Exception):
                await asyncio.shield(self.task)

    def router(self):
        router = APIRouter(prefix="/api/microlink-discovery", dependencies=[Depends(require_local)])

        @router.post("/estimate")
        def estimate(options: SearchOptions):
            return self.estimate(options, datetime.now(UTC).date())

        @router.post("/runs", status_code=202)
        async def start(options: SearchStart):
            return await self.start(options)

        @router.post("/resume", status_code=202)
        async def resume():
            return await self.resume()

        @router.get("/status")
        def status():
            return self.status()

        @router.post("/stop", status_code=202)
        async def stop():
            if self.task and not self.task.done():
                self.stop_requested.set()
                return {"status": "stopping"}
            return await asyncio.to_thread(self.status)

        return router
