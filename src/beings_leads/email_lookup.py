"""Explicit company email lookup; scans never call this service.

Reserve each attempt durably before contacting the provider. Uncertain attempts stay
reserved, so retries/double clicks cannot silently repeat a potentially billed request.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .models import ScannerExecutionSummary
from .store import LeadStore
from .treg_people import (
    CHAMPION_TITLES,
    DEFAULT_EMAIL_MAX_COST_USD,
    DEFAULT_PEOPLE_LIMIT,
    DISCOVERY_UNIT_COST_USD,
    ECONOMIC_TITLES,
    EXTRACTOR_VERSION,
    MAX_EMAIL_LIMIT,
    REFRESH_DAYS,
    SCANNER_ID,
    SCANNER_VERSION,
    ProviderPerson,
    TregPeopleError,
    email_candidates,
    project_people_result,
    select_people,
)

MAXIMUM_USD = DEFAULT_EMAIL_MAX_COST_USD * MAX_EMAIL_LIMIT


def lookup_failure(error):
    """Only allowlisted diagnostics reach storage/UI; raw exceptions may contain secrets."""
    descriptions = {
        "access_denied": "Treg denied access. Check the token's lookup permissions",
        "insufficient_balance": "Treg rejected the lookup because account credits are insufficient",
        "rate_limited": "The lookup is rate-limited; wait for the provider retry window",
        "provider_error": "Treg rejected the lookup",
        "timeout": "The provider request timed out",
        "connection_error": "The provider connection failed",
        "invalid_response": "The provider returned a response the app could not validate",
        "pending": "The provider accepted the request and is still processing it",
        "unsettled": "The provider has not confirmed the charge for this call",
        "unexpected_error": "An internal lookup error prevented result confirmation",
    }
    kind = (
        getattr(error, "kind", "unexpected_error")
        if isinstance(error, TregPeopleError)
        else "unexpected_error"
    )
    if kind not in descriptions:
        kind = "unexpected_error"
    status = getattr(error, "http_status", None)
    if not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    charged = getattr(error, "charged_microusd", None)
    if not isinstance(charged, int) or charged < 0:
        charged = None
    detail = descriptions[kind] + (f" (HTTP {status})" if status else "")
    return {
        "kind": kind,
        "http_status": status,
        "detail": detail,
        "charge_known": charged is not None,
        "charged_microusd": charged,
        "request_submitted": getattr(error, "request_submitted", None),
    }


def failure_message(failure, stage):
    return (
        f"{failure['detail']}. "
        + ("No email lookup ran. " if stage == "people" else "")
        + (
            "No provider request was submitted. "
            if failure.get("request_submitted") is False
            else (
                f"Provider reported ${failure['charged_microusd'] / 1_000_000:.6f} "
                "for this request. "
            )
            if failure["charge_known"]
            else "Charge status is unknown. "
        )
        + "Attempt remains reserved; no automatic retry."
    )


def preserve_unsettled_call(store, company, workflow, error):
    call_id = getattr(error, "call_id", "")
    if call_id:
        metadata = dict(getattr(error, "metadata", {}))
        if lookup_failure(error)["charge_known"]:
            metadata.update(settled=True, failure=lookup_failure(error))
        store.connection.execute(
            "INSERT OR IGNORE INTO treg_call_recovery "
            "(call_id,company_key,workflow,metadata_json,observed_at) VALUES (?,?,?,?,?)",
            (call_id, company, workflow, json.dumps(metadata), datetime.now(UTC).isoformat()),
        )


async def recover_pending_people(database, candidate_id, client):
    """Publish settled existing-call evidence locally; block duplicate paid discovery."""
    with LeadStore(database, initialize=False) as store:
        target = store.prospect_detail(candidate_id)
        if target is None:
            raise LookupError("Company not found")
        company = store._normalized_company_key(
            (target.get("target_intelligence") or {}).get("canonical_domain")
            or target.get("canonical_host")
            or target["hostname"]
        )
        rows = store.connection.execute(
            "SELECT * FROM treg_call_recovery WHERE company_key=? AND workflow='people' "
            "AND COALESCE(json_extract(metadata_json,'$.settled'),0)=0",
            (company,),
        ).fetchall()
    for row in rows:
        recover = getattr(client, "recover_people_search", None)
        result = await recover(row["call_id"], company) if recover else None
        if result is None:
            continue
        observed = datetime.fromisoformat(row["observed_at"])
        accepted = select_people(result.people, company_archetype=target.get("archetype"))
        projection = project_people_result(
            company_domain=company,
            people=accepted,
            observed_at=observed,
            search_call_ids=[result.call_id],
            response_hashes=[result.response_hash],
        )
        with LeadStore(database, initialize=False) as store:
            if store._candidate_company_keys([candidate_id]).get(candidate_id) != company:
                continue
            claim = store.claim_scanner_work(
                [candidate_id],
                scanner_id=SCANNER_ID,
                scanner_version=SCANNER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                scope_key="pending-recovery:" + result.call_id,
                config={"recovered_call": result.call_id},
                maximum=1,
                force_refresh=True,
            )
            if candidate_id not in claim["claimed"]:
                continue
            execution = claim["claimed"][candidate_id]
            store.complete_scanner_execution(
                execution,
                ScannerExecutionSummary(
                    execution_id=execution,
                    candidate_id=candidate_id,
                    company_key=company,
                    scanner_id=SCANNER_ID,
                    scanner_version=SCANNER_VERSION,
                    extractor_version=EXTRACTOR_VERSION,
                    status="success" if projection.observations else "success_empty",
                    request_count=0,
                    byte_count=result.response_bytes,
                    cost_microusd=0,
                    observation_count=len(projection.observations),
                    metadata={"recovered_call": result.call_id, "no_new_provider_call": True},
                ),
                coverage_manifests=projection.coverage_manifests,
                artifacts=projection.artifacts,
                observations=projection.observations,
                next_due_at=observed + timedelta(days=REFRESH_DAYS),
            )
            marker = store.connection.execute(
                "UPDATE company_email_lookups SET status='people_complete',"
                "cost_microusd=cost_microusd+? WHERE call_id=? "
                "AND person_key LIKE 'people-discovery:%' AND status='uncertain'",
                (result.charged_microusd, result.call_id),
            )
            existing_marker = store.connection.execute(
                "SELECT 1 FROM company_email_lookups WHERE call_id=? "
                "AND person_key LIKE 'people-discovery:%' LIMIT 1",
                (result.call_id,),
            ).fetchone()
            if not marker.rowcount and not existing_marker:
                from .spending import record_event

                record_event(
                    store.connection,
                    reference="treg-call:" + result.call_id,
                    service="treg",
                    amount=result.charged_microusd,
                    operation="Recovered people search",
                )
            metadata = json.loads(row["metadata_json"])
            metadata.update(settled=True, charged_micro=result.charged_microusd)
            store.connection.execute(
                "UPDATE treg_call_recovery SET metadata_json=? WHERE call_id=?",
                (json.dumps(metadata), result.call_id),
            )
            store.connection.commit()
    with LeadStore(database, initialize=False) as store:
        return not store.connection.execute(
            "SELECT 1 FROM treg_call_recovery WHERE company_key=? AND workflow='people' "
            "AND COALESCE(json_extract(metadata_json,'$.settled'),0)=0 LIMIT 1",
            (company,),
        ).fetchone()


def person_key(person):
    return str(person.get("linkedin_url") or "").lower().rstrip("/") or "|".join(
        str(person.get(key) or "").lower() for key in ("first_name", "last_name", "title")
    )


def overlay_email_results(store, company, people, *, now):
    rows = store.connection.execute(
        "SELECT * FROM company_email_lookups WHERE company_key=? "
        "AND observed_at<=? AND expires_at>? ORDER BY id",
        (company, now.isoformat(), now.isoformat()),
    ).fetchall()
    latest = {row["person_key"]: row for row in rows}
    for person in people:
        if (
            person.get("email_status") == "found"
            and person.get("email_verification_policy") != "mailbox-checked-v1"
        ):
            person["email_status"] = "unverified"
            person["email_provider_verified"] = False
        row = latest.get(person_key(person))
        if row is None:
            continue
        person.update(
            email_lookup_status=row["status"],
            email_lookup_at=row["observed_at"],
        )
        if row["status"] in {"found", "not_found"}:
            person.update(
                work_email=row["work_email"],
                email_status=(
                    "unverified"
                    if row["status"] == "found" and not row["email_verified"]
                    else row["status"]
                ),
                email_provider_verified=bool(row["email_verified"]),
                email_provider=row["provider"],
                email_call_id=row["call_id"],
            )


async def _release_reserved_emails(database, run_token, *, cancelled=False):
    """Release only this invocation's demonstrably unsent work; dispatched work stays held."""

    def release():
        now = datetime.now(UTC).isoformat()
        with LeadStore(database, initialize=False) as store, store.connection:
            store.connection.execute(
                "UPDATE company_email_lookups SET status='deferred',expires_at=? "
                "WHERE status='pending' AND call_id='' AND id IN "
                "(SELECT lookup_id FROM company_email_dispatch WHERE run_token=? "
                "AND dispatched_at IS NULL)",
                (now, run_token),
            )
            store.connection.execute(
                "UPDATE company_email_dispatch SET released_at=? WHERE run_token=? "
                "AND dispatched_at IS NULL",
                (now, run_token),
            )
            if cancelled:
                store.connection.execute(
                    "UPDATE company_email_lookups SET status='uncertain',error_json=? "
                    "WHERE status='pending' AND id IN "
                    "(SELECT lookup_id FROM company_email_dispatch WHERE run_token=? "
                    "AND dispatched_at IS NOT NULL)",
                    (json.dumps(lookup_failure(asyncio.CancelledError())), run_token),
                )

    task = asyncio.create_task(asyncio.to_thread(release))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def lookup_company_emails(database, candidate_id, client, *, allow_recent_empty=False):
    now = datetime.now(UTC)
    # Resolve previous requests from their archive, never by calling find again.
    with LeadStore(database, initialize=False) as store:
        target = store.prospect_detail(candidate_id)
        if target is None:
            raise LookupError("Company not found")
        company = store._normalized_company_key(
            (target.get("target_intelligence") or {}).get("canonical_domain")
            or target.get("canonical_host")
            or target["hostname"]
        )
        pending = store.connection.execute(
            "SELECT id,call_id FROM company_email_lookups WHERE company_key=? "
            "AND status IN ('pending','uncertain') AND call_id<>'' "
            "AND person_key NOT LIKE 'people-discovery:%'",
            (company,),
        ).fetchall()
    for row in pending:
        recover = getattr(client, "recover_work_email", None)
        result = await recover(row["call_id"]) if recover else None
        if result is None:
            continue
        with LeadStore(database, initialize=False) as store:
            store.connection.execute(
                "UPDATE company_email_lookups SET status=?,work_email=?,provider=?,"
                "cost_microusd=?,email_verified=? WHERE id=? AND status IN ('pending','uncertain')",
                (
                    "found"
                    if result.email and result.verified
                    else "unverified"
                    if result.candidate_present
                    else "not_found",
                    result.email if result.verified else "",
                    result.provider,
                    result.charged_microusd,
                    int(result.verified),
                    row["id"],
                ),
            )
            store.connection.commit()
    with LeadStore(database) as store:
        target = store.prospect_detail(candidate_id)
        if target is None:
            raise LookupError("Company not found")
        company = store._normalized_company_key(
            (target.get("target_intelligence") or {}).get("canonical_domain")
            or target.get("canonical_host")
            or target["hostname"]
        )
        intelligence = store.treg_people_intelligence(company)
        saved = intelligence["people"]
        selected = eligible_email_people(saved, target.get("archetype"))
        if not selected:
            if allow_recent_empty and intelligence.get("status") in {
                "fresh", "needs_more_contacts",
            }:
                return {
                    "attempted": 0,
                    "actual_cost_usd": 0,
                    "message": "Reused the recent people check. No eligible people were found.",
                }
            raise ValueError(
                "No current company-matched people are available. Run a people scan first."
            )
        saved_by_key = {person_key(p): p for p in saved}
        reservations = []
        run_token = uuid4().hex
        reused = 0
        saved_errors = []
        # Serialize reservations across requests/processes, without holding a DB lock
        # across a network call. Each reserved person can spend at most $0.025.
        store.connection.execute("BEGIN IMMEDIATE")
        for person in selected:
            key = person_key(
                {
                    "linkedin_url": person.linkedin_url,
                    "first_name": person.first_name,
                    "last_name": person.last_name,
                    "title": person.title,
                }
            )
            previous = saved_by_key.get(key, {})
            existing = store.connection.execute(
                "SELECT id,error_json FROM company_email_lookups "
                "WHERE company_key=? AND person_key=? "
                "AND expires_at>? LIMIT 1",
                (company, key, now.isoformat()),
            ).fetchone()
            if existing or previous.get("email_status") in {"found", "not_found"}:
                reused += 1
                if existing and existing["error_json"] != "{}":
                    saved_errors.append(json.loads(existing["error_json"]))
                continue
            row = store.connection.execute(
                "INSERT INTO company_email_lookups "
                "(company_key,person_key,status,observed_at,expires_at) VALUES (?,?,?,?,?)",
                (
                    company,
                    key,
                    "pending",
                    now.isoformat(),
                    (now + timedelta(days=REFRESH_DAYS)).isoformat(),
                ),
            )
            store.connection.execute(
                "INSERT INTO company_email_dispatch(lookup_id,run_token) VALUES(?,?)",
                (row.lastrowid, run_token),
            )
            reservations.append((row.lastrowid, person))
        store.connection.commit()
    charged = 0
    failures = 0
    error_details = []
    try:
        for lookup_id, person in reservations:
            with LeadStore(database, initialize=False) as dispatch_store:
                dispatch_store.connection.execute(
                    "UPDATE company_email_dispatch SET dispatched_at=? "
                    "WHERE lookup_id=? AND run_token=?",
                    (datetime.now(UTC).isoformat(), lookup_id, run_token),
                )
                dispatch_store.connection.commit()
            try:
                result = await client.find_work_email(
                    domain=company,
                    person=person,
                    max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
                    idempotency_key=f"company-email:{company}:{lookup_id}",
                )
            except Exception as exc:
                failure = lookup_failure(exc)
                error_details.append(failure)
                charged += failure["charged_microusd"] or 0
                with LeadStore(database) as store:
                    store.connection.execute(
                        "UPDATE company_email_lookups SET status=?,call_id=?,error_json=?,"
                        "cost_microusd=? WHERE id=?",
                        (
                            "failed" if failure["charge_known"] else "uncertain",
                            getattr(exc, "call_id", ""),
                            json.dumps(failure),
                            failure["charged_microusd"] or 0,
                            lookup_id,
                        ),
                    )
                    preserve_unsettled_call(store, company, "email", exc)
                    store.connection.commit()
                failures += 1
                continue
            if result.pending:
                failures += 1
            else:
                charged += result.charged_microusd
            with LeadStore(database) as store:
                store.connection.execute(
                    "UPDATE company_email_lookups SET status=?,work_email=?,provider=?,call_id=?,"
                    "cost_microusd=?,email_verified=? WHERE id=?",
                    (
                        "pending"
                        if result.pending
                        else "found"
                        if result.email and result.verified
                        else "unverified"
                        if result.candidate_present
                        else "not_found",
                        result.email if result.verified else "",
                        result.provider,
                        result.call_id,
                        result.charged_microusd,
                        int(result.verified),
                        lookup_id,
                    ),
                )
                store.connection.commit()
    except asyncio.CancelledError:
        await _release_reserved_emails(database, run_token, cancelled=True)
        raise
    finally:
        await _release_reserved_emails(database, run_token)
    with LeadStore(database) as store:
        people = store.treg_people_intelligence(company)
    if error_details:
        message = " ".join(
            dict.fromkeys(failure_message(error, "email") for error in error_details)
        )
    elif failures:
        message = (
            "Some email lookups are pending confirmation. "
            "Attempts remain reserved; no automatic retry."
        )
    elif reservations:
        message = "Email lookup complete."
    elif saved_errors:
        message = (
            "Saved failure: "
            + " ".join(dict.fromkeys(failure_message(error, "email") for error in saved_errors))
            + " No new provider request was submitted."
        )
    else:
        message = "Saved email results or reserved attempts reused; no new charge."
    return {
        "candidate_id": candidate_id,
        "company_key": company,
        "attempted": len(reservations),
        "reused": reused,
        "uncertain": sum(not error["charge_known"] for error in error_details)
        + failures
        - len(error_details),
        "failed": failures,
        "errors": error_details,
        "saved_errors": saved_errors,
        "actual_cost_usd": None
        if any(not error["charge_known"] for error in error_details)
        or failures > len(error_details)
        else charged / 1_000_000,
        "maximum_usd": float(MAXIMUM_USD),
        "people_intelligence": people,
        "message": message,
    }


# The same bounded two-lane discovery used by people hydration, only when needed.
PEOPLE_MAXIMUM_MICROUSD = int(DISCOVERY_UNIT_COST_USD * DEFAULT_PEOPLE_LIMIT * 1_000_000)
COMBINED_MAXIMUM_MICROUSD = int(MAXIMUM_USD * 1_000_000) + PEOPLE_MAXIMUM_MICROUSD
PEOPLE_ATTEMPT_KEY = "people-discovery:company-button:v1"


def eligible_email_people(saved, archetype=None):
    return email_candidates(
        select_people(
            [
                ProviderPerson(
                    first_name=p.get("first_name", ""),
                    last_name=p.get("last_name", ""),
                    title=p.get("title", ""),
                    linkedin_url=p.get("linkedin_url", ""),
                    company_name=p.get("company_name", ""),
                    provider=p.get("provider", "IcyPeas"),
                    company_domain_match=True,
                )
                for p in saved
                if p.get("company_domain_match") is True
                and p.get("first_name")
                and p.get("last_name")
            ],
            company_archetype=archetype,
        ),
        limit=MAX_EMAIL_LIMIT,
    )


def email_lookup_estimate(people):
    eligible = bool(eligible_email_people(people.get("people", [])))
    needed = not eligible and people.get("status") not in {"fresh", "needs_more_contacts"}
    maximum = COMBINED_MAXIMUM_MICROUSD if needed else int(MAXIMUM_USD * 1_000_000)
    return {
        "needs_people_scan": needed,
        "people_checked_without_candidates": not eligible and not needed,
        "maximum_microusd": maximum,
        "people_maximum_microusd": PEOPLE_MAXIMUM_MICROUSD if needed else 0,
        "email_maximum_microusd": int(MAXIMUM_USD * 1_000_000),
    }


def company_email_estimate(database, candidate_id):
    with LeadStore(database) as store:
        target = store.prospect_detail(candidate_id)
        if target is None:
            raise LookupError("Company not found")
        company = store._normalized_company_key(
            (target.get("target_intelligence") or {}).get("canonical_domain")
            or target.get("canonical_host")
            or target["hostname"]
        )
        estimate = email_lookup_estimate(store.treg_people_intelligence(company))
        active = store.connection.execute(
            "SELECT 1 FROM scanner_work WHERE company_key=? AND scanner_id=? "
            "AND status='running' AND lease_expires_at>? LIMIT 1",
            (company, SCANNER_ID, datetime.now(UTC).isoformat()),
        ).fetchone()
        return {**estimate, "people_scan_running": active is not None}


async def find_company_emails(database, candidate_id, client, *, confirmed_maximum_microusd):
    if not await recover_pending_people(database, candidate_id, client):
        return {
            "attempted": 0,
            "uncertain": 1,
            "actual_cost_usd": None,
            "message": "An existing people lookup is awaiting settlement. "
            "No new paid request was submitted.",
        }
    estimate = company_email_estimate(database, candidate_id)
    if confirmed_maximum_microusd < estimate["maximum_microusd"]:
        raise ValueError("Lookup maximum changed; reload the company")
    charged = 0
    scanned = False
    if estimate["needs_people_scan"]:
        now = datetime.now(UTC)
        with LeadStore(database) as store:
            target = store.prospect_detail(candidate_id)
            company = store._normalized_company_key(
                (target.get("target_intelligence") or {}).get("canonical_domain")
                or target.get("canonical_host")
                or target["hostname"]
            )
            store.connection.execute("BEGIN IMMEDIATE")
            previous = store.connection.execute(
                "SELECT status,error_json FROM company_email_lookups "
                "WHERE company_key=? AND person_key=? "
                "AND expires_at>? ORDER BY id DESC LIMIT 1",
                (company, PEOPLE_ATTEMPT_KEY, now.isoformat()),
            ).fetchone()
            if previous:
                store.connection.rollback()
                saved_error = json.loads(previous["error_json"] or "{}")
                return {
                    "attempted": 0,
                    "actual_cost_usd": 0,
                    "maximum_usd": estimate["maximum_microusd"] / 1_000_000,
                    "error": saved_error or None,
                    "message": "Saved failure: " + failure_message(saved_error, "people")
                    if saved_error
                    else "Previous people lookup is unconfirmed; its original error "
                    "detail was not recorded. No new provider request was submitted."
                    if previous["status"] == "uncertain"
                    else "People lookup already attempted or in progress; "
                    "no new charge. No current eligible people are available.",
                }
            attempt = store.connection.execute(
                "INSERT INTO company_email_lookups(company_key,person_key,status,observed_at,"
                "expires_at,maximum_microusd) VALUES(?,?,?,?,?,?)",
                (
                    company,
                    PEOPLE_ATTEMPT_KEY,
                    "pending",
                    now.isoformat(),
                    (now + timedelta(days=REFRESH_DAYS)).isoformat(),
                    PEOPLE_MAXIMUM_MICROUSD,
                ),
            ).lastrowid
            store.connection.commit()
            claim = store.claim_scanner_work(
                [candidate_id],
                scanner_id=SCANNER_ID,
                scanner_version=SCANNER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                maximum=1,
                force_refresh=True,
                scope_key="company-button",
                config={"people_limit": DEFAULT_PEOPLE_LIMIT, "email_limit": 0},
            )
            execution = claim["claimed"].get(candidate_id)
            if execution is None:
                # No request was made; allow a later click after the existing scan finishes.
                store.connection.execute(
                    "UPDATE company_email_lookups SET status='deferred',expires_at=? WHERE id=?",
                    (now.isoformat(), attempt),
                )
                store.connection.commit()
                return {
                    "attempted": 0,
                    "actual_cost_usd": 0,
                    "waiting_for_people": candidate_id in claim["active_candidate_ids"],
                    "message": (
                        "People scan is already running for this company; waiting for contacts."
                        if candidate_id in claim["active_candidate_ids"]
                        else "People lookup is held by its reuse or provider retry policy. "
                        "No new provider request was submitted."
                    ),
                }
        people, call_ids, hashes = [], [], []
        requests = byte_count = 0
        try:
            for lane, titles, limit in (
                ("economic", ECONOMIC_TITLES, 3),
                ("champion", CHAMPION_TITLES, 5),
            ):
                result = await client.search_people(
                    domain=company,
                    titles=titles,
                    limit=limit,
                    idempotency_key=f"company-people:{attempt}:{execution}:{lane}",
                )
                charged += result.charged_microusd
                requests += 1
                byte_count += result.response_bytes
                people.extend(result.people)
                call_ids.append(result.call_id)
                hashes.append(result.response_hash)
            accepted = select_people(
                people, company_archetype=target.get("archetype"), limit=DEFAULT_PEOPLE_LIMIT
            )
            projection = project_people_result(
                company_domain=company,
                people=accepted,
                search_call_ids=call_ids,
                response_hashes=hashes,
            )
            with LeadStore(database) as store:
                if store._candidate_company_keys([candidate_id]).get(candidate_id) != company:
                    raise ValueError("Company identity changed during people discovery")
                store.complete_scanner_execution(
                    execution,
                    ScannerExecutionSummary(
                        execution_id=execution,
                        candidate_id=candidate_id,
                        company_key=company,
                        scanner_id=SCANNER_ID,
                        scanner_version=SCANNER_VERSION,
                        extractor_version=EXTRACTOR_VERSION,
                        status="success" if projection.observations else "success_empty",
                        request_count=requests,
                        byte_count=byte_count,
                        cost_microusd=charged,
                        observation_count=len(projection.observations),
                    ),
                    coverage_manifests=projection.coverage_manifests,
                    artifacts=projection.artifacts,
                    observations=projection.observations,
                    next_due_at=now + timedelta(days=REFRESH_DAYS),
                )
                store.connection.execute(
                    "UPDATE company_email_lookups SET status='people_complete',"
                    "cost_microusd=? WHERE id=?",
                    (charged, attempt),
                )
                store.connection.commit()
            scanned = True
        except Exception as exc:
            failure = lookup_failure(exc)
            charged += failure["charged_microusd"] or 0
            with LeadStore(database) as store:
                store.connection.execute(
                    "UPDATE company_email_lookups SET status=?,"
                    "cost_microusd=?,call_id=?,error_json=? WHERE id=?",
                    (
                        "failed" if failure["charge_known"] else "uncertain",
                        charged,
                        getattr(exc, "call_id", ""),
                        json.dumps(failure),
                        attempt,
                    ),
                )
                preserve_unsettled_call(store, company, "people", exc)
                store.connection.commit()
                store.fail_scanner_execution(
                    execution, status="transient_error", error=failure_message(failure, "people")
                )
            return {
                "attempted": 0,
                "uncertain": int(not failure["charge_known"]),
                "failed": 1,
                "error": failure,
                "actual_cost_usd": charged / 1_000_000 if failure["charge_known"] else None,
                "message": failure_message(failure, "people"),
            }
        if company_email_estimate(database, candidate_id)["people_checked_without_candidates"]:
            return {
                "attempted": 0,
                "people_scanned": True,
                "actual_cost_usd": charged / 1_000_000,
                "message": "People scan complete; no eligible company-matched people found. "
                "No email lookup charge.",
            }
    result = await lookup_company_emails(
        database, candidate_id, client, allow_recent_empty=True,
    )
    result.update(
        people_scanned=scanned,
        actual_cost_usd=(
            None
            if result["actual_cost_usd"] is None
            else result["actual_cost_usd"] + charged / 1_000_000
        ),
        maximum_usd=estimate["maximum_microusd"] / 1_000_000,
    )
    return result
