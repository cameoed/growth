"""Shared request controls. Only current control metadata lives in the active database.

Provider quotas remain authoritative; defaults below are conservative application caps,
not claims about a customer's plan. No paid request is automatically replayed.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .async_work import run_thread_owned
from .scan_diagnostics import SOURCE, record_http, source_time

DATABASE: ContextVar[Path | None] = ContextVar("provider_rate_database", default=None)
CANCELLED: ContextVar[threading.Event | None] = ContextVar("provider_cancelled", default=None)
REQUEST_ACTIVITY: ContextVar[Callable[[str, str], None] | None] = ContextVar(
    "request_activity", default=None,
)
_CLIENT_POOL = ContextVar("client_request_pool", default=None)
MAX_WAIT_SECONDS = 10.0
LEASE_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 900.0
_sleep = asyncio.sleep


def notify_request_activity(phase: str, url: str) -> None:
    observer = REQUEST_ACTIVITY.get()
    if observer is not None:
        observer(phase, str(url))


@asynccontextmanager
async def client_pool_slots(client, maximum):
    token = _CLIENT_POOL.set((client, asyncio.Semaphore(maximum)))
    try:
        yield
    finally:
        _CLIENT_POOL.reset(token)


@asynccontextmanager
async def _pool_slot(client):
    pool = _CLIENT_POOL.get()
    if pool is None or pool[0] is not client:
        yield
    else:
        async with pool[1]:
            yield


class ProviderRateLimitDeferred(httpx.HTTPStatusError):
    """Preflight rejection: no provider request was dispatched."""


def is_local_rate_deferral(error) -> bool:
    response = getattr(error, "response", None)
    return isinstance(error, ProviderRateLimitDeferred) or (
        response is not None and response.extensions.get("beings_request_deferred") is True
    )


def paid_call(call, repository, reservation_key, **kwargs):
    """Settle known-unsent work at zero; retain uncertainty for actual network failures."""
    try:
        return call(**kwargs)
    except Exception as exc:
        if is_local_rate_deferral(exc):
            repository.settle_budget(reservation_key, 0)
        raise


async def provider_thread(call, *args, **kwargs):
    """Cancel queued synchronous SDK work before it can dispatch a paid request."""
    cancelled = threading.Event()
    token = CANCELLED.set(cancelled)
    try:
        return await asyncio.to_thread(call, *args, **kwargs)
    except asyncio.CancelledError:
        cancelled.set()
        raise
    finally:
        CANCELLED.reset(token)


@dataclass(frozen=True)
class Policy:
    name: str
    interval: float = 0
    concurrent: int = 0
    requests: int = 0
    window: float = 60
    quota: bool = False


class MicrolinkWindow:
    """Learn from selected work only; no paid probes, extra requests or retries.

    Fast growth after a full successful window, immediate halving on pressure, and
    a recovery interval prevent late successes from undoing a burst of failures.
    The shared process-owned network lane is the hard cross-process resource bound.
    """

    INITIAL = 32
    MAXIMUM = 128
    RECOVERY_SECONDS = 30

    def __init__(self):
        self.lock = threading.Lock()
        self.limit = self.INITIAL
        self.active = 0
        self.successes = 0
        self.recover_at = 0.0

    def acquire(self):
        with self.lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    def release(self, response, error, *, now):
        with self.lock:
            self.active -= 1
            pressure = (isinstance(error, httpx.TransportError)
                        or (response is not None and response.status_code in {
                            408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530,
                        } and not is_local_rate_deferral(error)
                            and not response.extensions.get("beings_request_deferred")))
            if pressure:
                self.limit = max(1, self.limit // 2)
                self.successes = 0
                self.recover_at = now + self.RECOVERY_SECONDS
            elif response is not None and response.is_success and now >= self.recover_at:
                self.successes += 1
                if self.successes >= self.limit:
                    self.limit = min(self.MAXIMUM, self.limit * 2)
                    self.successes = 0


def policy_for(request: httpx.Request, provider: str | None = None) -> Policy:
    host = request.url.host
    if provider == "openai" or host == "api.openai.com":
        return Policy("OpenAI", 3, 1, 20)
    if host == "pro.microlink.io":
        # Paid plans publish quota limits, not a parallel-request throttle. The
        # application's shared network admission still bounds local resource use.
        return Policy("Microlink", quota=True)
    if host == "api.microlink.io":
        return Policy("Microlink free", concurrent=1, requests=25, window=86400)
    if provider == "dataforseo" or host == "api.dataforseo.com":
        return Policy("DataForSEO", .06, 10, 1000)
    if provider == "treg" or host == "treg.to" or "x-treg-token" in request.headers:
        return Policy("Treg", 1, 2, 60)
    if provider == "apify" or host == "api.apify.com":
        return Policy("Apify", .1, 10, 600)
    if host == "open.tiktokapis.com":
        return Policy("TikTok", 1, 1, 60)
    if host == "api.gdeltproject.org":
        return Policy("GDELT", 5, 1, 12)
    if host == "query.wikidata.org":
        return Policy("Wikidata", 60, 1, 1)
    if host == "overpass-api.de":
        return Policy("Overpass", 5, 1, 12)
    if host in {"index.commoncrawl.org", "crt.name", "dns.google", "cloudflare-dns.com"}:
        dns = host in {"dns.google", "cloudflare-dns.com"}
        return Policy(host, .02 if dns else 1, 10 if dns else 1)
    if host == "api.github.com":
        return Policy("GitHub", concurrent=5,
                      requests=4500 if request.headers.get("authorization") else 60, window=3600)
    return Policy(host)


def bucket_for(request: httpx.Request, policy: Policy) -> str:
    credentials = "|".join(request.headers.get(name, "") for name in (
        "authorization", "x-api-key", "x-treg-token", "x-treg-org"))
    # The account fingerprint is control identity, never a stored credential or URL/query.
    fingerprint = hashlib.sha256(credentials.encode()).hexdigest()[:32] if credentials else "public"
    return f"{policy.name}:{fingerprint}"


def _number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def retry_after_seconds(headers, *, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    milliseconds = _number(headers.get("retry-after-ms"))
    raw = headers.get("retry-after")
    numeric = _number(raw)
    values = []
    if milliseconds is not None:
        values.append(milliseconds / 1000)
    if numeric is not None:
        values.append(numeric)
    elif raw:
        with suppress(TypeError, ValueError, OverflowError):
            values.append(max(0, parsedate_to_datetime(raw).timestamp() - now))
    return max(values) if values else None


def _reset(raw, now: float, *, duration: bool = False) -> float | None:
    numeric = _number(raw)
    if numeric is not None:
        return now + numeric if duration else numeric
    if duration and isinstance(raw, str):
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", raw)
        if parts and "".join(number + unit for number, unit in parts) == raw:
            units = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}
            return now + sum(float(number) * units[unit] for number, unit in parts)
    return None


def dataforseo_rate_limit_code(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    tasks = payload.get("tasks")
    codes = [payload.get("status_code"), *(task.get("status_code")
        for task in (tasks if isinstance(tasks, list) else []) if isinstance(task, dict))]
    return next((str(code) for code in codes if str(code) in {"40202", "40209"}), None)


def response_control(response: httpx.Response, policy: Policy, *, now: float) -> tuple:
    headers = response.headers
    hold = 0.0
    reason = ""
    remaining = None
    reset = 0.0
    prefix = "x-rate-limit" if policy.name.startswith("Microlink") else "x-ratelimit"
    if policy.name == "OpenAI":
        remaining = _number(headers.get("x-ratelimit-remaining-requests"))
        reset = _reset(headers.get("x-ratelimit-reset-requests"), now, duration=True) or 0
        for metric in ("tokens", "project-tokens"):
            if _number(headers.get(f"x-ratelimit-remaining-{metric}")) == 0:
                hold = max(hold, _reset(headers.get(f"x-ratelimit-reset-{metric}"), now,
                                       duration=True) or now + DEFAULT_COOLDOWN_SECONDS)
                reason = "token rate limit exhausted"
    else:
        remaining = _number(headers.get(f"{prefix}-remaining"))
        reset = _reset(headers.get(f"{prefix}-reset"), now) or 0
    if remaining is not None and remaining <= 0:
        hold = max(hold, reset if reset > now else now + DEFAULT_COOLDOWN_SECONDS)
        reason = "provider quota exhausted"
    body_limited = False
    if policy.name == "DataForSEO" and response.is_stream_consumed:
        with suppress(ValueError, AttributeError, TypeError):
            body_limited = dataforseo_rate_limit_code(response.json()) is not None
    delay = retry_after_seconds(headers, now=now)
    if response.status_code == 429 or body_limited or (
        response.status_code == 403 and remaining == 0
    ):
        hold = max(hold, now + (delay if delay is not None else DEFAULT_COOLDOWN_SECONDS), reset)
        reason = "provider rate limited the request"
    elif response.status_code == 503 and delay is not None:
        hold = max(hold, now + delay)
        reason = "provider requested a retry delay"
    return hold, reason, remaining, reset


class Ledger:
    def __init__(self, database: Path | None):
        self.database = database
        self.lock = threading.RLock()
        self.memory = (
            sqlite3.connect(":memory:", check_same_thread=False) if database is None else None
        )
        self.initialized = False
        self.microlink_windows = {}

    @contextmanager
    def transaction(self, *, write=True):
        # LeadStore participates in the application's maintenance lock and never clones data.
        with self.lock:
            if self.database is None:
                connection = self.memory
                owner = None
            else:
                from .store import LeadStore
                owner = LeadStore(self.database, initialize=False)
                connection = owner.connection
            try:
                connection.execute("BEGIN IMMEDIATE" if write or not self.initialized else "BEGIN")
                if not self.initialized:
                    connection.execute("""CREATE TABLE IF NOT EXISTS provider_request_controls (
                        bucket TEXT PRIMARY KEY, next_start REAL NOT NULL DEFAULT 0,
                        blocked_until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                        remaining REAL, quota_reset REAL NOT NULL DEFAULT 0)""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS provider_request_permits (
                        id TEXT PRIMARY KEY, bucket TEXT NOT NULL, started REAL NOT NULL,
                        expires REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
                    connection.execute("""CREATE INDEX IF NOT EXISTS idx_provider_permits_bucket
                        ON provider_request_permits(bucket, started)""")
                yield connection
                connection.commit()
                self.initialized = True
            except BaseException:
                connection.rollback()
                raise
            finally:
                if owner:
                    owner.close()

    def acquire(self, bucket: str, policy: Policy, *, now: float) -> tuple[str | None, float, str]:
        if not (policy.concurrent or policy.requests or policy.interval or policy.quota):
            with self.transaction(write=False) as connection:
                row = connection.execute(
                    "SELECT blocked_until,reason FROM provider_request_controls WHERE bucket=?",
                    (bucket,)).fetchone()
                if row and row[0] > now:
                    return None, row[0] - now, row[1]
            if row:
                with self.transaction() as connection:
                    connection.execute("DELETE FROM provider_request_controls "
                        "WHERE bucket=? AND blocked_until<=? AND quota_reset<=? AND next_start<=?",
                        (bucket, now, now, now))
            return "", 0, ""
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM provider_request_permits WHERE active=0 AND expires<=?", (now,)
            )
            connection.execute(
                "UPDATE provider_request_permits SET active=0,expires=started+86400 "
                "WHERE active=1 AND expires<=?", (now,)
            )
            connection.execute("DELETE FROM provider_request_permits WHERE bucket=? "
                "AND active=0 AND started<=?", (bucket, now - policy.window))
            connection.execute("DELETE FROM provider_request_controls WHERE next_start<=? "
                "AND blocked_until<=? AND quota_reset<=?", (now, now, now))
            row = connection.execute("SELECT * FROM provider_request_controls WHERE bucket=?",
                                     (bucket,)).fetchone()
            if row:
                _, next_start, blocked, reason, remaining, reset = tuple(row)
                if blocked > now:
                    return None, blocked - now, reason
                if remaining is not None and reset > now and remaining <= 0:
                    return None, reset - now, "provider quota exhausted"
                if next_start > now:
                    return None, next_start - now, "local request pacing"
            if policy.requests:
                count, earliest = connection.execute(
                    "SELECT COUNT(*),MIN(started) FROM provider_request_permits "
                    "WHERE bucket=? AND started>?", (bucket, now - policy.window)).fetchone()
                if count >= policy.requests:
                    return None, earliest + policy.window - now, "local request window limit"
            if policy.concurrent:
                active = connection.execute("SELECT COUNT(*) FROM provider_request_permits "
                    "WHERE bucket=? AND active=1", (bucket,)).fetchone()[0]
                if active >= policy.concurrent:
                    return None, .1, "local concurrency limit"
            identifier = uuid.uuid4().hex
            connection.execute("INSERT INTO provider_request_permits VALUES(?,?,?,?,1)",
                               (identifier, bucket, now, now + LEASE_SECONDS))
            connection.execute("""INSERT INTO provider_request_controls(bucket,next_start)
                VALUES(?,?) ON CONFLICT(bucket) DO UPDATE SET next_start=excluded.next_start,
                remaining=CASE WHEN quota_reset>? THEN MAX(0,remaining-1) ELSE NULL END""",
                (bucket, now + policy.interval, now))
            return identifier, 0, ""

    def settle(self, bucket: str, identifier: str, policy: Policy,
               response: httpx.Response | None, *, now: float) -> None:
        control = response_control(response, policy, now=now) if response is not None else None
        if not identifier and (control is None or (not control[0] and control[2] is None)):
            return
        with self.transaction() as connection:
            if control is not None:
                hold, reason, remaining, reset = control
                if remaining is not None and reset > now:
                    active = connection.execute("SELECT COUNT(*) FROM provider_request_permits "
                        "WHERE bucket=? AND active=1 AND id<>?", (bucket, identifier)).fetchone()[0]
                    remaining = max(0, remaining - active)
                if hold or remaining is not None:
                    connection.execute("""INSERT INTO provider_request_controls
                        (bucket,blocked_until,reason,remaining,quota_reset) VALUES(?,?,?,?,?)
                        ON CONFLICT(bucket) DO UPDATE SET
                        blocked_until=MAX(blocked_until,excluded.blocked_until),
                        reason=CASE WHEN excluded.blocked_until>=blocked_until
                          THEN excluded.reason ELSE reason END,
                        remaining=CASE WHEN quota_reset>? AND remaining IS NOT NULL
                          AND excluded.remaining IS NOT NULL THEN MIN(remaining,excluded.remaining)
                          ELSE COALESCE(excluded.remaining,remaining) END,
                        quota_reset=MAX(quota_reset,excluded.quota_reset)""",
                        (bucket, hold, reason, remaining, reset, now))
            if identifier:
                if policy.requests:
                    connection.execute("UPDATE provider_request_permits "
                        "SET active=0,expires=started+? WHERE id=?", (policy.window, identifier))
                else:
                    connection.execute("DELETE FROM provider_request_permits WHERE id=?",
                                       (identifier,))

    def renew(self, identifier: str, *, now: float) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE provider_request_permits SET expires=? "
                "WHERE id=? AND active=1", (now + LEASE_SECONDS, identifier))


_ledgers: dict[tuple, Ledger] = {}
_ledger_lock = threading.Lock()


def ledger_for(client) -> Ledger:
    database = DATABASE.get()
    if database is not None:
        database = database.resolve()
        stat = database.stat()  # Never create a guessed database or use production in tests.
        key = str(database), stat.st_dev, stat.st_ino
        with _ledger_lock:
            if key not in _ledgers:
                _ledgers[key] = Ledger(database)
            return _ledgers[key]
    ledger = getattr(client, "_beings_provider_ledger", None)
    if ledger is None:
        ledger = Ledger(None)
        client._beings_provider_ledger = ledger
    return ledger


@contextmanager
def rate_limit_scope(database):
    token = DATABASE.set(Path(database))
    try:
        yield
    finally:
        DATABASE.reset(token)


def deferred_response(request: httpx.Request, policy: Policy, delay: float, reason: str):
    wait = max(1, math.ceil(delay))
    message = (f"{policy.name} HTTP 429: rate limited; request deferred locally "
               f"({reason}); retry after {wait} seconds")
    return httpx.Response(429, request=request,
        headers={"retry-after": str(wait), "x-beings-request-deferred": "1"},
        extensions={"beings_request_deferred": True},
        json={"error": {"message": message, "type": "rate_limit_error", "code": "rate_limited"}})


def _request_identity(client, method, url, kwargs):
    request = client.build_request(method, url, headers=kwargs.get("headers"))
    auth = kwargs.get("auth", client.auth)
    if isinstance(auth, httpx.BasicAuth):
        request = next(auth.auth_flow(request))
    return request


@asynccontextmanager
async def _permit(client, request, provider=None):
    policy = policy_for(request, provider)
    ledger = ledger_for(client)
    bucket = bucket_for(request, policy)
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    metrics = SOURCE.get()
    admission_started = time.monotonic() if metrics else 0
    while True:
        acquisition = asyncio.create_task(asyncio.to_thread(
            ledger.acquire, bucket, policy, now=time.time()))
        try:
            identifier, delay, reason = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            while not acquisition.done():
                try:
                    await asyncio.shield(acquisition)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not acquisition.cancelled() and acquisition.exception() is None:
                identifier, _, _ = acquisition.result()
                if identifier is not None:
                    await run_thread_owned(
                        ledger.settle, bucket, identifier, policy, None, now=time.time(),
                    )
            raise
        if identifier is not None:
            break
        if delay > MAX_WAIT_SECONDS or time.monotonic() >= deadline:
            if metrics:
                metrics.record("local_rate_deferral", time.monotonic() - admission_started)
            response = deferred_response(request, policy, delay, reason)
            raise ProviderRateLimitDeferred(response.json()["error"]["message"],
                                            request=request, response=response)
        await _sleep(min(delay, .2))
    receipt = [None]

    async def renew_lease():
        while True:
            await _sleep(LEASE_SECONDS / 3)
            await asyncio.to_thread(ledger.renew, identifier, now=time.time())

    renewal = asyncio.create_task(renew_lease()) if identifier else None
    try:
        yield receipt
    finally:
        if renewal is not None:
            renewal.cancel()
        try:
            # A competing SQLite writer can make settlement slow. Await owned local I/O
            # without freezing the loop or abandoning the release on a second stop signal.
            with source_time("request_settlement"):
                await run_thread_owned(
                    ledger.settle, bucket, identifier, policy, receipt[0], now=time.time(),
                )
        finally:
            if renewal is not None:
                with suppress(asyncio.CancelledError):
                    await renewal


@asynccontextmanager
async def _admission(client, request, provider=None, before_dispatch=None):
    from .scan_resources import async_resource

    if request.url.host != "pro.microlink.io":
        async with _permit(client, request, provider) as receipt, async_resource("network"):
            yield receipt
        return
    ledger = ledger_for(client)
    bucket = bucket_for(request, policy_for(request, provider))
    with ledger.lock:
        window = ledger.microlink_windows.setdefault(bucket, MicrolinkWindow())
    while not window.acquire():
        await _sleep(.02)
    receipt, error = [None], None
    try:
        # Quota admission happens immediately before dispatch, after resource
        # waits, so a queued request cannot bypass a newly reported provider hold.
        async with async_resource("microlink_network"):
            if before_dispatch is not None:
                await before_dispatch()
            async with _permit(client, request, provider) as owned:
                receipt = owned
                yield receipt
    except BaseException as exc:
        error = exc
        raise
    finally:
        window.release(receipt[0], error, now=time.monotonic())


async def provider_request(client, method, url, *, provider=None, **kwargs):
    identity = _request_identity(client, method, url, kwargs)
    metrics = SOURCE.get()
    started = time.monotonic() if metrics else 0
    async with _pool_slot(client), _admission(client, identity, provider) as receipt:
        if metrics:
            metrics.record("request_admission", time.monotonic() - started)
        with source_time("request_transport"):
            response = await getattr(client, method.lower())(url, **kwargs)
            record_http(response)
        receipt[0] = response
        return response


@asynccontextmanager
async def provider_stream(client, method, url, *, provider=None, before_dispatch=None, **kwargs):
    identity = _request_identity(client, method, url, kwargs)
    notify_request_activity("admission", url)
    metrics = SOURCE.get()
    started = time.monotonic() if metrics else 0
    async with (
        _pool_slot(client), _admission(client, identity, provider, before_dispatch) as receipt,
    ):
        if metrics:
            metrics.record("request_admission", time.monotonic() - started)
        notify_request_activity("network", url)
        try:
            with source_time("stream_transport"):
                async with client.stream(method, url, **kwargs) as response:
                    receipt[0] = response
                    record_http(response)
                    yield response
        except asyncio.CancelledError:
            notify_request_activity("cancelled", url)
            raise
        finally:
            notify_request_activity("idle", url)


class ProviderHTTPClient(httpx.Client):
    """Public send boundary used by every OpenAI SDK request, with no automatic replay."""
    def send(self, request, **kwargs):
        from .scan_resources import resource

        policy = policy_for(request, "openai")
        ledger = ledger_for(self)
        bucket = bucket_for(request, policy)
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        while True:
            cancelled = CANCELLED.get()
            if cancelled is not None and cancelled.is_set():
                return deferred_response(request, policy, 0, "cancelled before dispatch")
            identifier, delay, reason = ledger.acquire(bucket, policy, now=time.time())
            if identifier is not None:
                break
            if delay > MAX_WAIT_SECONDS or time.monotonic() >= deadline:
                return deferred_response(request, policy, delay, reason)
            time.sleep(min(delay, .2))
        response = None
        finished = threading.Event()
        renewal_errors = []

        def renew_lease():
            while not finished.wait(LEASE_SECONDS / 3):
                try:
                    ledger.renew(identifier, now=time.time())
                except Exception as exc:
                    renewal_errors.append(exc)
                    return

        renewal = threading.Thread(target=renew_lease, daemon=True)
        renewal.start()
        try:
            cancelled = CANCELLED.get()
            if cancelled is not None and cancelled.is_set():
                return deferred_response(request, policy, 0, "cancelled before dispatch")
            with resource("network"):
                cancelled = CANCELLED.get()
                if cancelled is not None and cancelled.is_set():
                    return deferred_response(request, policy, 0, "cancelled before dispatch")
                response = super().send(request, **kwargs)
                return response
        finally:
            finished.set()
            renewal.join()
            ledger.settle(bucket, identifier, policy, response, now=time.time())
            if renewal_errors:
                raise RuntimeError("Provider request lease renewal failed") from renewal_errors[0]
