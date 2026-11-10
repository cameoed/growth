"""Usage-cost accounting and independent, read-only provider billing reports.

Amounts are integer micro-USD. Provider account reports never augment app receipts:
accounts may serve other projects. Token-priced app receipts are estimates, not invoices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from hashlib import sha256
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .treg_people import resolve_treg_credentials

SERVICES = {
    "openai": (
        "OpenAI",
        "Usage estimates include historic search fees; older token costs are missing.",
    ),
    "apify": ("Apify", "Provider-reported actor costs; excludes duplicated query totals."),
    "dataforseo": ("DataForSEO", "Provider-reported Smart Scan SEO charges."),
    "treg": ("Treg", "People and email usage charges; may consume promotional credits."),
    "microlink": ("Microlink", "Subscription dollars are not recorded by rendering requests."),
}
DOCS = {
    "openai": "https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs",
    "apify": "https://docs.apify.com/api/v2/users-me-usage-monthly-get",
    "dataforseo": "https://docs.dataforseo.com/v3/appendix-user-data/",
    "treg": "https://treg.to/docs",
    "microlink": "https://microlink.io/pricing",
}
OPENAI_ORGANIZATION = "https://api.openai.com/v1/organization"


def _key_fingerprint(key):
    return sha256(key.encode()).hexdigest() if key else ""


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS spending_provider_reports (
        service TEXT NOT NULL, report_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(service, report_key))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS spending_usage_events (
        reference TEXT PRIMARY KEY, service TEXT NOT NULL, amount_microusd INTEGER NOT NULL,
        estimated INTEGER NOT NULL, operation TEXT NOT NULL, observed_at TEXT NOT NULL)""")


def record_event(connection, *, reference, service, amount, estimated=False, operation=""):
    initialize(connection)
    connection.execute(
        """INSERT OR IGNORE INTO spending_usage_events
        (reference,service,amount_microusd,estimated,operation,observed_at)
        VALUES(?,?,?,?,?,?)""",
        (reference, service, amount, int(estimated), operation, datetime.now(UTC).isoformat()),
    )


def record_openai_response(response, model):
    """New unbudgeted bulk/scout calls only; never persist prompts or credentials."""
    from .provider_rate_limit import DATABASE

    database = DATABASE.get()
    usage = getattr(response, "usage", None)
    identifier = getattr(response, "id", None)
    if database is None or usage is None or not isinstance(identifier, str):
        return
    rates = next(
        (
            rates
            for name, rates in (
                ("gpt-5-mini", (Decimal("0.25"), Decimal("0.025"), Decimal("2"))),
                ("gpt-5-nano", (Decimal("0.05"), Decimal("0.005"), Decimal("0.4"))),
            )
            if model == name or model.startswith(name + "-20")
        ),
        None,
    )
    # Non-default tiers and unknown models have no verified price here.
    if rates is None or getattr(response, "service_tier", None) not in {None, "default", "auto"}:
        return
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    values = (usage.input_tokens, usage.output_tokens, cached)
    if any(type(value) is not int or value < 0 for value in values) or cached > usage.input_tokens:
        return
    searches = sum(
        getattr(item, "type", None) == "web_search_call" for item in getattr(response, "output", [])
    )
    cost = (
        Decimal(usage.input_tokens - cached) * rates[0]
        + Decimal(cached) * rates[1]
        + Decimal(usage.output_tokens) * rates[2]
    ) / 1_000_000 + Decimal(searches) * Decimal("0.01")
    try:
        with sqlite3.connect(database, timeout=30) as connection:
            record_event(
                connection,
                reference="openai:" + identifier,
                service="openai",
                amount=micro(cost),
                estimated=True,
                operation="Unbudgeted " + model,
            )
    except (sqlite3.Error, OSError):
        logging.getLogger(__name__).warning("Unable to persist OpenAI usage cost metadata")


def micro(value) -> int:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite billing amount")
    return int((number * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _date(value) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _rows(connection, table, columns, suffix="", args=()):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone():
        return []
    return [
        dict(row) for row in connection.execute(f"SELECT {columns} FROM {table} {suffix}", args)
    ]


def local_events(connection):
    """One authoritative receipt per call; do not sum copied cache/parent projections."""
    events = []

    def add(service, timestamp, amount=0, reserved=0, estimated=False, source=""):
        if timestamp and (amount or reserved):
            events.append(
                {
                    "service": service,
                    "timestamp": timestamp,
                    "microusd": int(amount or 0),
                    "reserved_microusd": int(reserved or 0),
                    "estimated": estimated,
                    "source": source,
                    "aggregate": source in {"SEO run aggregate", "Historic search fees"},
                }
            )

    seo_by_run = {}
    for row in _rows(connection, "spending_usage_events", "*"):
        add(
            row["service"],
            row["observed_at"],
            row["amount_microusd"],
            estimated=bool(row["estimated"]),
            source="Usage receipt",
        )
        if row["service"] == "dataforseo":
            seo_by_run[row["operation"]] = (
                seo_by_run.get(row["operation"], 0) + row["amount_microusd"]
            )

    first_live_usage = (
        connection.execute(
            "SELECT MIN(observed_at) FROM spending_usage_events WHERE service='openai'"
        ).fetchone()[0]
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='spending_usage_events'"
        ).fetchone()
        else None
    )
    for row in _rows(connection, "source_scout_runs", "model,finished_at,summary_json"):
        # Reconstruct only confirmed historic search fees, never guessed tokens.
        # Runs ending after live receipt capture starts are already represented there.
        if row["model"] != "gpt-5-mini" or not row["finished_at"]:
            continue
        if first_live_usage and _date(row["finished_at"]) >= _date(first_live_usage):
            continue
        metadata = json.loads(row["summary_json"] or "{}")
        searches = metadata.get("actual_web_search_calls")
        if type(searches) is int and searches > 0:
            add(
                "openai",
                row["finished_at"],
                searches * 10_000,
                estimated=True,
                source="Historic search fees",
            )

    for row in _rows(connection, "engine_budget_reservations", "*"):
        service = row["provider"].lower()
        if service not in SERVICES:
            continue
        add(
            service,
            row["updated_at"],
            row["actual_microusd"] if row["status"] == "settled" else 0,
            row["maximum_microusd"] if row["status"] == "reserved" else 0,
            service == "openai",
            "Budget receipt",
        )
    for row in _rows(
        connection,
        "ad_discovery_runs",
        "actual_cost_microusd,started_at,status,approved_cap_microusd",
    ):
        add(
            "apify",
            row["started_at"],
            row["actual_cost_microusd"],
            max(0, row["approved_cap_microusd"] - row["actual_cost_microusd"])
            if row["status"] in {"running", "cancel_requested", "uncertain"}
            else 0,
            source="Paid-ad run",
        )
    # The company-button discovery marker mirrors its scanner execution, so count
    # the marker once and omit company-button scanner copies below.
    for row in _rows(
        connection, "company_email_lookups", "status,observed_at,cost_microusd,maximum_microusd"
    ):
        add(
            "treg",
            row["observed_at"],
            row["cost_microusd"],
            max(0, row["maximum_microusd"] - row["cost_microusd"])
            if row["status"] in {"pending", "uncertain"}
            else 0,
            source="People/email lookup",
        )
    for row in _rows(
        connection,
        "scanner_executions",
        "cost_microusd,finished_at,started_at",
        "WHERE scanner_id=? AND scope_key<>?",
        ("treg_people_intelligence_v1", "company-button"),
    ):
        add(
            "treg",
            row["finished_at"] or row["started_at"],
            row["cost_microusd"],
            source="People hydration",
        )
    for row in _rows(
        connection,
        "buyer_collection_receipts",
        "id,actual_microusd,maximum_microusd,observed_at,status,request_key,coverage_json",
    ):
        # A settled budget reservation, when present, owns the cost.
        if connection.execute(
            "SELECT 1 FROM engine_budget_reservations WHERE idempotency_key=?",
            (row["id"],),
        ).fetchone():
            continue
        coverage = json.loads(row["coverage_json"] or "{}")
        provider = str(coverage.get("provider") or "treg").lower()
        if provider in SERVICES:
            add(
                provider,
                row["observed_at"],
                row["actual_microusd"],
                row["maximum_microusd"]
                if row["status"] in {"reserved", "running", "uncertain"}
                else 0,
                provider == "openai",
                "Collection receipt",
            )
    for row in _rows(connection, "smart_scan_runs", "id,state_json,started_at,updated_at"):
        state = json.loads(row["state_json"])
        add(
            "dataforseo",
            row["updated_at"] or row["started_at"],
            max(
                0,
                micro(state.get("actual_dataforseo_cost_usd") or 0) - seo_by_run.get(row["id"], 0),
            ),
            source="SEO run aggregate",
        )
    return events


def summary(connection, period="all", *, now=None):
    now = now or datetime.now(UTC)
    local_now = now.astimezone(ZoneInfo("America/New_York"))
    cutoff = (
        local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "month"
        else now - timedelta(days=30)
        if period == "30d"
        else None
    )
    totals = {
        key: {
            "service": key,
            "name": name,
            "recorded_microusd": 0,
            "estimated_microusd": 0,
            "reserved_microusd": 0,
            "receipt_count": 0,
            "note": note,
            "coverage": "partial",
        }
        for key, (name, note) in SERVICES.items()
    }
    daily = {}
    events = local_events(connection)
    unallocated = 0
    for event in events:
        stamp = _date(event["timestamp"])
        if cutoff and event.get("aggregate"):
            unallocated += event["microusd"]
            continue
        if stamp > now or (cutoff and stamp < cutoff):
            continue
        service = event["service"]
        row = totals[service]
        field = "estimated_microusd" if event["estimated"] else "recorded_microusd"
        row[field] += event["microusd"]
        row["reserved_microusd"] += event["reserved_microusd"]
        row["receipt_count"] += 1
        if event["microusd"] and not event.get("aggregate"):
            day = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
            item = daily.setdefault(
                (day, service),
                {"date": day, "service": service, "recorded_microusd": 0, "estimated_microusd": 0},
            )
            item[field] += event["microusd"]
    rows = list(totals.values())
    reports = [
        json.loads(row["payload_json"])
        for row in _rows(
            connection,
            "spending_provider_reports",
            "payload_json",
            "ORDER BY service,report_key",
        )
    ]
    research_key = _key_fingerprint(os.getenv("OPENAI_API_KEY"))
    reports = [
        report for report in reports
        if report["service"] != "openai"
        or (
            report.get("scope") == "api_key"
            and report.pop("credential_fingerprint", None) == research_key
        )
    ]
    return {
        "currency": "USD",
        "timezone": "America/New_York",
        "period": period,
        "as_of": now.isoformat(),
        "services": rows,
        "recorded_microusd": sum(row["recorded_microusd"] for row in rows),
        "estimated_microusd": sum(row["estimated_microusd"] for row in rows),
        "reserved_microusd": sum(row["reserved_microusd"] for row in rows),
        "unallocated_microusd": unallocated,
        "undated_microusd": sum(
            event["microusd"]
            for event in events
            if event.get("aggregate") and _date(event["timestamp"]) <= now and not cutoff
        ),
        "daily": sorted(
            daily.values(), key=lambda row: (row["date"], row["service"]), reverse=True
        ),
        "provider_reports": reports,
    }


def save_reports(connection, reports):
    initialize(connection)
    stamp = datetime.now(UTC).isoformat()
    owns_transaction = not connection.in_transaction
    connection.execute("SAVEPOINT spending_reports")
    try:
        for report in reports:
            report_key = report["report_key"]
            if "credential_fingerprint" in report:
                report_key += ":" + report["credential_fingerprint"]
            connection.execute(
                """INSERT INTO spending_provider_reports VALUES(?,?,?,?)
                ON CONFLICT(service,report_key) DO UPDATE SET
                payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (report["service"], report_key, json.dumps(report), stamp),
            )

        connection.execute("RELEASE spending_reports")
        if owns_transaction:
            connection.commit()
    except BaseException:
        connection.execute("ROLLBACK TO spending_reports")
        connection.execute("RELEASE spending_reports")
        raise


async def collect_reports(*, earliest, environ=None, client=None, treg_credentials=None):
    """Only free documented billing GETs. Failures never erase previous history."""
    env = dict(os.environ if environ is None else environ)
    research_key = env.get("OPENAI_API_KEY")
    now = datetime.now(UTC)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
    treg = treg_credentials or resolve_treg_credentials(environ=env)
    stamp = now.isoformat()

    def base(service, key="status", **data):
        return {
            "service": service,
            "name": SERVICES[service][0],
            "report_key": key,
            "scope": "provider_account",
            "as_of": stamp,
            "source_url": DOCS[service],
            **(
                {"scope": "api_key", "credential_fingerprint": _key_fingerprint(research_key)}
                if service == "openai" else {}
            ),
            **data,
        }

    async def get(url, **kwargs):
        response = await client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def openai_directory(path, headers, **params):
        seen = set()
        while True:
            payload = await get(
                OPENAI_ORGANIZATION + path, headers=headers, params={"limit": 100, **params},
            )
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("data"), list)
                or type(payload.get("has_more")) is not bool
            ):
                raise ValueError("Incomplete OpenAI key directory")
            for item in payload["data"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                ):
                    raise ValueError("Invalid OpenAI directory record")
                yield item
            if not payload["has_more"]:
                break
            cursor = payload.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("OpenAI directory pagination did not advance")
            seen.add(cursor)
            params["after"] = cursor

    async def openai_research_key_id(headers):
        matches = set()
        async for project in openai_directory("/projects", headers, include_archived=True):
            path = f"/projects/{quote(project['id'], safe='')}/api_keys"
            async for item in openai_directory(path, headers, owner_project_access="any"):
                masked = item.get("redacted_value")
                if not isinstance(masked, str):
                    raise ValueError("Missing redacted OpenAI key")
                parts = re.fullmatch(
                    r"(sk-[A-Za-z0-9_-]*)(?:\.{3}|…|\*{3,})([A-Za-z0-9_-]{3,})", masked,
                )
                if parts:
                    prefix, suffix = parts.groups()
                    if (
                        len(research_key) > len(prefix) + len(suffix)
                        and research_key.startswith(prefix) and research_key.endswith(suffix)
                    ):
                        matches.add(item["id"])
        return next(iter(matches)) if len(matches) == 1 else None

    async def apify():
        if not env.get("APIFY_API_TOKEN"):
            return [base("apify", status="unavailable", note="API token not configured.")]
        results = []
        cursor = _date(earliest).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cycles = set()
        missing = 0
        while cursor <= now:
            try:
                payload = await get(
                    "https://api.apify.com/v2/users/me/usage/monthly",
                    headers={"Authorization": "Bearer " + env["APIFY_API_TOKEN"]},
                    params={"date": cursor.date().isoformat()},
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {400, 404}:
                    raise
                missing += 1
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                continue
            data = payload["data"]
            cycle = data["usageCycle"]
            key = cycle["startAt"]
            if key not in cycles:
                cycles.add(key)
                results.append(
                    base(
                        "apify",
                        key,
                        status="available",
                        basis="usage_credits",
                        amount_microusd=micro(data["totalUsageCreditsUsdAfterVolumeDiscount"]),
                        period_start=key,
                        period_end=cycle["endAt"],
                        note=(
                            "Account usage credits; includes other projects "
                            "and excludes subscription invoices."
                        ),
                        daily=[
                            {
                                "date": item["date"][:10],
                                "amount_microusd": micro(item["totalUsageCreditsUsd"]),
                            }
                            for item in data.get("dailyServiceUsages", [])
                        ],
                    )
                )
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return results + [
            base(
                "apify",
                status="available",
                note=(
                    "Historical billing cycles since this app's first scan."
                    + (f" {missing} monthly reports unavailable." if missing else "")
                ),
            )
        ]

    async def seo():
        if not env.get("DATAFORSEO_LOGIN") or not env.get("DATAFORSEO_PASSWORD"):
            return [base("dataforseo", status="unavailable", note="Credentials not configured.")]
        payload = await get(
            "https://api.dataforseo.com/v3/appendix/user_data",
            auth=(env["DATAFORSEO_LOGIN"], env["DATAFORSEO_PASSWORD"]),
        )
        if payload.get("status_code") != 20000:
            raise ValueError("Provider did not return a valid usage report")
        money = payload["tasks"][0]["result"][0]["money"]
        deposited, balance = micro(money["total"]), micro(money["balance"])
        day = money.get("statistics", {}).get("day", {})
        return [
            base(
                "dataforseo",
                "account",
                status="available",
                basis="reconstructed",
                amount_microusd=deposited - balance,
                deposited_microusd=deposited,
                balance_microusd=balance,
                period_start=None,
                period_end=stamp,
                note=(
                    "Deposits minus balance: account-wide net credits consumed, "
                    "not app attribution or cash invoices."
                ),
                daily=[{"date": str(day["value"])[:10], "amount_microusd": micro(day["total"])}]
                if "total" in day and "value" in day
                else [],
            )
        ]

    async def wallet():
        if treg is None:
            return [base("treg", status="unavailable", note="Treg not configured.")]
        headers = {"X-Treg-Token": treg.token}
        headers["X-Treg-Org"] = treg.org or ""
        org = treg.org if str(treg.org or "").isdigit() else None
        if not org:
            try:
                organizations = await get(treg.base_url + "/orgs", headers=headers)
                selected = next(
                    (item for item in organizations if item.get("slug") == treg.org), None
                )
                if selected is None and not treg.org:
                    selected = next((item for item in organizations if item.get("active")), None)
                org = selected.get("org_id") if selected else None
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {401, 403}:
                    raise
                me = await get(treg.base_url + "/auth/me", headers=headers)
                org = me.get("org_id")
        if not org:
            return [base("treg", status="unavailable", note="No matching active organization.")]
        offset, seen, daily, balance, oldest = 0, set(), {}, None, None
        while True:
            payload = await get(
                treg.base_url + "/orgs/" + quote(str(org), safe="") + "/balance",
                headers=headers,
                params={"limit": 100, "offset": offset},
            )
            balance = int(payload["balance_micro"])
            entries = payload["entries"]["items"]
            new = 0
            for entry in entries:
                if entry["id"] in seen:
                    continue
                new += 1
                seen.add(entry["id"])
                oldest = min(oldest or entry["created_at"], entry["created_at"])
                # Reservations/releases are temporary holds, not charges.
                if entry["kind"] in {"settle", "refund"}:
                    day = entry["created_at"][:10]
                    daily[day] = daily.get(day, 0) - int(entry["amount_micro"])
            if len(entries) < int(payload["entries"]["limit"]):
                break
            if not new:
                raise ValueError("Billing pagination did not advance")
            offset += len(entries)
        reports = [
            base(
                "treg",
                "account",
                status="available",
                basis="usage_credits",
                amount_microusd=sum(daily.values()),
                balance_microusd=balance,
                period_start=oldest,
                period_end=stamp,
                note=(
                    "Settled org usage, net of refunds; excludes holds/top-ups "
                    "and may use promotional credits."
                ),
                daily=[
                    {"date": day, "amount_microusd": value} for day, value in sorted(daily.items())
                ],
            )
        ]
        # Keep the complete account ledger separate from app-tag attribution.
        # The provider caps this endpoint at 365 days; never call it all-history.
        try:
            attribution = await get(
                treg.base_url + "/orgs/" + quote(str(org), safe="") + "/usage/by-tag",
                headers=headers, params={"key": "workspace", "days": 365},
            )
            row = next((r for r in attribution["rows"]
                        if r.get("value") == "beings-growth"), None)
            if row is not None and isinstance(row.get("charged_micro"), int):
                reports.append(base(
                    "treg", "app-attribution", status="available",
                    basis="attributed_usage", scope="app_workspace",
                    amount_microusd=row["charged_micro"],
                    unattributed_microusd=attribution.get("unattributed_micro"),
                    period_start=attribution.get("since"), period_end=stamp,
                    note="App-tagged actual charges within the provider's 365-day window. "
                         "Untagged charges remain unassigned; overlaps account total.",
                ))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        return reports

    async def openai():
        key = env.get("OPENAI_ADMIN_KEY")
        if not key:
            return [
                base(
                    "openai",
                    status="unavailable",
                    error_code="admin_key_required",
                    note=(
                        "Add an OpenAI admin key in Settings → OpenAI billing, "
                        "then refresh Spend. The research key cannot read organization costs."
                    ),
                )
            ]
        if not research_key:
            return [base(
                "openai", status="unavailable", error_code="research_key_required",
                note="Add the OpenAI research key in Settings, then refresh Spend.",
            )]
        headers = {"Authorization": "Bearer " + key}
        key_id = await openai_research_key_id(headers)
        if key_id is None:
            return [base(
                "openai", status="unavailable", error_code="research_key_unmatched",
                note=(
                    "Could not uniquely match the research key to a project key "
                    "in the billing key's organization."
                ),
            )]
        page, seen, buckets, daily = None, set(), set(), {}
        # Costs are daily UTC buckets; record the exact requested boundary.
        start_date = _date(earliest).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = int(start_date.timestamp()), int(now.timestamp())
        while True:
            params = {
                "start_time": start, "end_time": end, "bucket_width": "1d", "limit": 180,
                "api_key_ids[]": key_id, "group_by[]": "api_key_id",
            }
            if page:
                params["page"] = page
            payload = await get(
                OPENAI_ORGANIZATION + "/costs",
                headers=headers,
                params=params,
            )
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("data"), list)
                or type(payload.get("has_more")) is not bool
            ):
                raise ValueError("Incomplete Costs response")
            for bucket in payload["data"]:
                if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
                    raise ValueError("Invalid Costs bucket")
                timestamp = bucket.get("start_time")
                if (
                    type(timestamp) is not int or not start <= timestamp < end
                    or timestamp in buckets
                ):
                    raise ValueError("Invalid or repeated Costs bucket")
                buckets.add(timestamp)
                day = datetime.fromtimestamp(timestamp, UTC).date().isoformat()
                for result in bucket["results"]:
                    if not isinstance(result, dict) or not isinstance(result.get("amount"), dict):
                        raise ValueError("Invalid Costs result")
                    if result.get("api_key_id") != key_id:
                        raise ValueError("Costs result is outside the requested key scope")
                    amount = result["amount"]
                    if str(amount["currency"]).lower() != "usd":
                        raise ValueError("Cost report contains a non-USD currency")
                    value = Decimal(str(amount["value"]))
                    if not value.is_finite():
                        raise ValueError("Non-finite Costs amount")
                    daily[day] = daily.get(day, Decimal(0)) + value
            if not payload["has_more"]:
                break
            page = payload.get("next_page")
            if not isinstance(page, str) or not page or page in seen:
                raise ValueError("Billing pagination did not advance")
            seen.add(page)
        daily = {day: micro(value) for day, value in daily.items()}
        return [
            base(
                "openai",
                "api_key",
                status="available",
                basis="provider_reported",
                amount_microusd=sum(daily.values()),
                period_start=start_date.isoformat(),
                period_end=datetime.fromtimestamp(end, UTC).isoformat(),
                coverage="partial",
                note=(
                    "Usage costs attributed to the configured research key "
                    "since the first scan's UTC day."
                ),
                daily=[
                    {"date": day, "amount_microusd": value} for day, value in sorted(daily.items())
                ],
            )
        ]

    async def isolated(service, operation):
        try:
            reports = await operation()
            if not any(row["report_key"] == "status" for row in reports):
                reports.append(base(service, status="available", note="Billing history retrieved."))
            return reports
        except httpx.HTTPStatusError as exc:
            if service == "openai":
                code, note = {
                    401: (
                        "invalid_admin_key",
                        "OpenAI rejected the billing key. Replace it in Settings → OpenAI billing.",
                    ),
                    403: (
                        "billing_access_denied",
                        "OpenAI billing access denied. Use an admin key with permission "
                        "to read project keys and costs in Settings → OpenAI billing.",
                    ),
                    429: ("rate_limited", "OpenAI billing is rate limited. Retry refresh later."),
                }.get(exc.response.status_code, (
                    "billing_unavailable",
                    f"OpenAI billing unavailable (HTTP {exc.response.status_code}). Retry refresh.",
                ))
                return [base(service, status="unavailable", error_code=code, note=note)]
            return [
                base(
                    service,
                    status="unavailable",
                    note=f"Billing report unavailable (HTTP {exc.response.status_code}).",
                )
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, DecimalException):
            return [
                base(
                    service,
                    status="unavailable",
                    note=("Billing report unavailable or incomplete; saved history retained."),
                )
            ]

    try:
        results = await asyncio.gather(
            *[
                isolated(service, operation)
                for service, operation in (
                    ("apify", apify),
                    ("dataforseo", seo),
                    ("treg", wallet),
                    ("openai", openai),
                )
            ]
        )
        return [row for group in results for row in group] + [
            base(
                "microlink",
                status="unavailable",
                note=(
                    "No documented invoice API established for this key; "
                    "subscription spending is unknown."
                ),
            )
        ]
    finally:
        if owns_client:
            await client.aclose()
