"""Durable failure allocation, separate from evidence freshness and buyer fit."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

FAILURE_LIMIT = 3
PUBLIC_FETCH_SECONDS = 20.0
WINDOW = timedelta(hours=1)
HOLDS = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
PUBLIC_NETWORK_SCANNERS = frozenset({
    "lifecycle_email_exhaust_v1", "app_release_pulse_v1", "creator_program_contract_v1",
    "owned_media_pulse_v1", "corporate_blog_engine_v1", "creative_hiring_demand_v1",
    "product_release_engine_v1", "event_webinar_engine_v1", "trust_center_readiness_v1",
    "brand_asset_governance_v1", "public_design_system_v1",
})
_CURRENT_BUDGET = ContextVar("company_public_research_budget", default=None)
LOCAL_CAPACITY_DEFERRED = "local_capacity_deferred"


@contextmanager
def public_requests(budget):
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield
    finally:
        _CURRENT_BUDGET.reset(token)


def bounded_public_request(operation):
    """Apply a company budget only inside an explicitly owned enrichment scope."""
    @wraps(operation)
    async def bounded(client, url, *args, **kwargs):
        budget = _CURRENT_BUDGET.get()
        if budget is None:
            return await operation(client, url, *args, **kwargs)
        from .provider_rate_limit import REQUEST_ACTIVITY
        from .retrieval_policy import scanner_host_hold_seconds

        budget.check()
        previous = REQUEST_ACTIVITY.get()
        phase = ""

        def observe(value, target):
            nonlocal phase
            phase = value
            if previous:
                previous(value, target)

        callback = kwargs.get("before_dispatch")

        async def before_dispatch():
            budget.check()  # A queued sibling may have opened the hold meanwhile.
            if callback:
                await callback()

        token = REQUEST_ACTIVITY.set(observe)
        try:
            async with asyncio.timeout(PUBLIC_FETCH_SECONDS):
                response, body = await operation(
                    client, url, *args, **{**kwargs, "before_dispatch": before_dispatch})
        except Exception as exc:
            if isinstance(exc, httpx.PoolTimeout) or (
                isinstance(exc, TimeoutError) and phase == "admission"
            ):
                raise CompanyResearchDeferred(
                    "Request admission deferred; company not penalized") from exc
            if substantive_failure(exc):
                budget.failure(minimum_hold=scanner_host_hold_seconds(exc, minimum=0))
            raise
        finally:
            REQUEST_ACTIVITY.reset(token)
        auxiliary = urlsplit(str(url)).path in {
            "/robots.txt", "/.well-known/assetlinks.json",
            "/.well-known/apple-app-site-association", "/apple-app-site-association",
        }
        if response.status_code >= 400 and not auxiliary:
            error = httpx.HTTPStatusError("Public research page failed",
                                          request=response.request, response=response)
            if substantive_failure(error):
                budget.failure(minimum_hold=scanner_host_hold_seconds(error, minimum=0))
        elif response.is_success and body and not auxiliary:
            budget.success()
        return response, body
    return bounded


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS company_research_backoff(
        company_key TEXT NOT NULL, lane TEXT NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0, window_started_at TEXT NOT NULL,
        hold_count INTEGER NOT NULL DEFAULT 0, hold_until TEXT,
        PRIMARY KEY(company_key,lane))""")


class CompanyResearchDeferred(httpx.RequestError):
    terminal_status = "transient_error"
    company_research_deferred = True


def substantive_failure(error, *, paid=False):
    from .provider_rate_limit import is_local_rate_deferral

    if (isinstance(error, httpx.PoolTimeout)
        or getattr(error, "company_research_deferred", False) or is_local_rate_deferral(error)):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 400
    return (isinstance(error, (httpx.TransportError, TimeoutError))
            or getattr(error, "terminal_status", "") == "robots_blocked"
            or (paid and isinstance(error, ValueError)))


@contextmanager
def _transaction(connection):
    nested = connection.in_transaction
    name = "research_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.commit()
    except BaseException:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.rollback()
        raise


class CompanyResearchBudget:
    def __init__(self, connection, company_key, *, lane="public"):
        self.connection = connection
        self.company_key = company_key.lower().rstrip(".").removeprefix("www.")
        self.lane = lane

    def _row(self):
        return self.connection.execute(
            "SELECT failures,window_started_at,hold_count,hold_until "
            "FROM company_research_backoff WHERE company_key=? AND lane=?",
            (self.company_key, self.lane),
        ).fetchone()

    def held_until(self, *, now=None):
        now = now or datetime.now(UTC)
        row = self._row()
        until = datetime.fromisoformat(row[3]) if row and row[3] else None
        return until if until and until > now else None

    def check(self):
        if until := self.held_until():
            raise CompanyResearchDeferred(
                f"Company research deferred after repeated failures until {until.isoformat()}")

    def failure(self, *, now=None, minimum_hold=0.0):
        now = now or datetime.now(UTC)
        with _transaction(self.connection):
            row = self._row()
            if row and row[3] and datetime.fromisoformat(row[3]) > now:
                return  # A skipped request cannot extend an existing hold.
            expired = row and row[3] and datetime.fromisoformat(row[3]) <= now
            fresh_window = (not row or expired
                            or datetime.fromisoformat(row[1]) + WINDOW <= now)
            failures = 1 if fresh_window else row[0] + 1
            start = now.isoformat() if fresh_window else row[1]
            count = row[2] if row else 0
            until = None
            if failures >= FAILURE_LIMIT:
                until = now + max(HOLDS[min(count, len(HOLDS)-1)],
                                  timedelta(seconds=minimum_hold))
                count += 1
            self.connection.execute(
                "INSERT INTO company_research_backoff VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(company_key,lane) DO UPDATE SET failures=excluded.failures,"
                "window_started_at=excluded.window_started_at,hold_count=excluded.hold_count,"
                "hold_until=excluded.hold_until",
                (self.company_key, self.lane, failures, start, count,
                 until.isoformat() if until else None),
            )

    def success(self, *, now=None):
        now = now or datetime.now(UTC)
        row = self._row()
        # Within a window this is a cumulative failure budget. A genuine successful
        # retry after cooldown (or a quiet full window) clears the old escalation.
        if row and ((row[3] and datetime.fromisoformat(row[3]) <= now)
                    or (not row[3] and datetime.fromisoformat(row[1]) + WINDOW <= now)):
            with _transaction(self.connection):
                current = self._row()
                if current and tuple(current) == tuple(row):
                    self.connection.execute(
                        "DELETE FROM company_research_backoff WHERE company_key=? AND lane=?",
                        (self.company_key, self.lane),
                    )
