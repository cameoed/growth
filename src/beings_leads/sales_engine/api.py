"""Local operator API for the sales workbench; collection remains explicitly controlled."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ConfigDict, Field, model_validator

from ..email_lookup import company_email_estimate, find_company_emails
from ..local_access import require_local
from ..microlink_contacts_scan import (
    REQUESTS_PER_COMPANY,
    company_contact_crawl_target,
    crawl_company_contacts,
)
from ..missions.spec import MissionCompile, MissionCreate
from ..store import LeadStore
from ..treg_people import TregPeopleClient, resolve_treg_credentials
from .buyer import BuyerFact
from .maintenance import empirical_scanner_yield, mission_write_lock
from .models import (
    BudgetPolicy,
    EngineModel,
    ProductionOpportunity,
    RelationshipEvidence,
    ResearchReview,
)
from .providers import MAXIMUM_MICROUSD, MODEL, analyze_visual
from .ranking import refresh_rankings
from .ranking_modes import RankingMode
from .service import SalesEngineService
from .visual import CreativeAnnotation
from .workload import CreativeWindow, WorkloadEvent

logger = logging.getLogger("uvicorn.error").getChild("mission")
MISSION_REQUEST_HEADER = "X-Mission-Request-ID"


class MissionUpdate(EngineModel):
    archived: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_update(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Mission updates cannot be null")
        if not self.model_fields_set:
            self.archived = True  # Preserve the original empty-body archive contract.
        return self


class MissionReviewStart(EngineModel):
    quote_id: str = Field(min_length=64, max_length=64)
    confirmed_maximum_microusd: int | None = Field(default=None, ge=0)


class VisualRequest(EngineModel):
    source_url: str = Field(max_length=2048)
    image_base64: str = Field(max_length=5_600_000)
    confirmed_maximum_microusd: int


class BuyerCollectionQuote(EngineModel):
    company_limit: int = Field(default=50, ge=1, le=250)
    maximum_microusd: int = Field(default=1_000_000, ge=1, le=5_000_000)


class BuyerCollectionStart(EngineModel):
    confirmed_maximum_microusd: int = Field(ge=0, le=5_000_000)


class WatchRequest(EngineModel):
    active: bool = True


class EmailLookupRequest(EngineModel):
    confirmed_maximum_microusd: Literal[0, 50000, 53040]
    confirmed_microlink_requests: int = Field(default=0, ge=0, le=REQUESTS_PER_COMPANY)


class ContactIdentity(EngineModel):
    person_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    mission_id: str = Field(default="beings", min_length=1, max_length=100)


class AIContactLookup(ContactIdentity):
    confirmed_maximum_microusd: Literal[200000]
    retry_cache_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WorkPhoneLookup(ContactIdentity):
    confirmed_maximum_microusd: Literal[5000, 5495] | None = None
    manual_retry: bool = False


class DecisionRequest(EngineModel):
    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"^[\w.:-]+$")


class BenchmarkRequest(EngineModel):
    human_concept_labels: dict[str, str] = Field(max_length=200)


def _conditional_response(request: Request, payload: dict, etag: str | None = None):
    if etag is None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    matches = request.headers.get("if-none-match", "").split(",")
    if any(value.strip().removeprefix("W/") in {"*", etag} for value in matches):
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


class OutboundSave(EngineModel):
    candidate_id: int = Field(gt=0)
    person_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    mission_id: str = Field(default="beings", min_length=1, max_length=100)


class OutboundDetails(EngineModel):
    name: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    profile_url: str = Field(default="", max_length=2048)


class OutboundManual(OutboundDetails):
    candidate_id: int = Field(gt=0)


class OutboundNote(EngineModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    note: str = Field(max_length=100_000)


class OutboundContactMethod(EngineModel):
    contact_method: Literal["", "email", "linkedin", "phone", "other"]


class OutboundTouchpoint(EngineModel):
    contact_method: Literal["email", "linkedin", "phone", "other"]


class OutboundStatus(EngineModel):
    status: Literal["saved", "contacted", "replied", "interested", "meeting_booked",
                    "closed_won", "not_interested", "do_not_contact"]


class TagRescore(EngineModel):
    tag: str = Field(min_length=1, max_length=40)
    mission_id: str = Field(default="beings", min_length=1, max_length=100)


class CompanyTagChange(EngineModel):
    tag: str = Field(min_length=1, max_length=40)
    action: Literal["add", "remove"]


def _mission_write_busy(error, *, saved):
    code = getattr(error, "sqlite_errorcode", 0)
    if (code & 0xFF) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise error
    detail = (
        "The mission was saved. Open Missions to check its AI setup status before retrying."
        if saved else "The database is busy. Your mission was not saved. Try again shortly."
    )
    return HTTPException(503, detail, headers={"Retry-After": "5"})


@contextmanager
def _mission_request(request, response, operation):
    supplied = request.headers.get(MISSION_REQUEST_HEADER, "")
    ref = (
        supplied if len(supplied) == 12 and all(c in "0123456789abcdef" for c in supplied)
        else uuid4().hex[:12]
    )
    response.headers[MISSION_REQUEST_HEADER] = ref
    started = time.monotonic()
    progress = {"stage": "open"}
    logger.info("mission_request ref=%s operation=%s event=begin", ref, operation)
    try:
        yield progress
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__, limit=-1)
        site = f"{frames[0].name}:{frames[0].lineno}" if frames else "-"
        logger.warning(
            "mission_request ref=%s operation=%s stage=%s elapsed_ms=%.0f error=%s "
            "sqlite_code=%s status=%s site=%s",
            ref, operation, progress["stage"], (time.monotonic() - started) * 1000,
            type(exc).__name__, getattr(exc.__cause__ or exc, "sqlite_errorcode", None),
            getattr(exc, "status_code", 500), site,
        )
        if isinstance(exc, HTTPException):
            exc.headers = {**(exc.headers or {}), MISSION_REQUEST_HEADER: ref}
            raise
        raise HTTPException(
            500, "Mission save status unavailable." if operation == "create"
            else "Mission build status unavailable.",
            headers={MISSION_REQUEST_HEADER: ref},
        ) from None
    else:
        logger.info(
            "mission_request ref=%s operation=%s event=complete elapsed_ms=%.0f "
            "compilation_status=%s compiler_error=%s",
            ref, operation, (time.monotonic() - started) * 1000,
            progress.get("compilation_status"), progress.get("compiler_error"),
        )


def create_router(database_path):
    router = APIRouter(prefix="/api/sales-engine", dependencies=[Depends(require_local)])

    def tagged_response(request, engine, payload):
        from ..company_tags import company_tags
        payload = {**payload, "tags": company_tags(engine.store, payload["candidate_id"])
                   if payload.get("candidate_id") else []}
        return _conditional_response(request, payload)

    @router.post("/companies/{candidate_id}/tags")
    def change_tag(candidate_id: int, change: CompanyTagChange):
        from ..company_tags import change_company_tag
        with LeadStore(database_path()) as store:
            try:
                return change_company_tag(store, candidate_id, change.tag, change.action)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/tags/rescore")
    def rescore_selected_tag(request: TagRescore):
        from ..company_tags import rescore_tag
        with LeadStore(database_path()) as store:
            try:
                return rescore_tag(store, request.tag, request.mission_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/outbound")
    def outbound_list(status: str = "", search: str = Query(default="", max_length=500),
                      offset: int = Query(default=0, ge=0),
                      limit: int = Query(default=50, ge=1, le=100)):
        from .outbound import list_people
        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            try:
                return list_people(store.connection, status=status, search=search,
                                   offset=offset, limit=limit)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/outbound/export.csv")
    def outbound_export():
        from .outbound import export_people_csv

        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            content = export_people_csv(store.connection).encode("utf-8-sig")
        return Response(
            content,
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="prospects.csv"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/outbound/company/{candidate_id}")
    def outbound_company(candidate_id: int):
        from .outbound import list_people
        from .summaries import _identity
        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            try:
                identity = _identity(store.connection, candidate_id)
                # The contact card has at most ten people; saved company identities
                # are not capped at the current recommendation limit.
                results = list_people(store.connection, company_key=identity["company_key"],
                                      limit=2147483647)
                return {"items": [{"id": r["id"], "person_key": r["person_key"],
                                   "name": r["name"], "profile_url": r["profile_url"],
                                   "status": r["status"], "email": r["email"], "title": r["title"],
                                   "phone": r["phone"], "ai_contact": r["ai_contact"],
                                   "public_work_phone": r["public_work_phone"],
                                   "phone_lookup_blocked": r["phone_lookup_blocked"],
                                   "operator_supplied": r["operator_supplied"]}
                                  for r in results["items"]]}
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.post("/outbound")
    def outbound_save(payload: OutboundSave):
        from .outbound import save
        with LeadStore(database_path(), initialize=False) as store:
            engine = SimpleNamespace(store=store, connection=store.connection)
            try:
                if payload.mission_id == "beings":
                    from .summaries import company_summary
                    packet, _ = company_summary(engine, payload.candidate_id)
                else:
                    from ..missions.reads import summary
                    packet = summary(engine, payload.mission_id, payload.candidate_id)
                return save(store.connection, packet, payload.person_key)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/companies/{candidate_id}/contacts/find-with-ai")
    def contact_lookup(candidate_id: int, payload: AIContactLookup):
        from ..provider_rate_limit import rate_limit_scope
        from .contact_lookup import find_contacts
        from .persistence import SalesEngineRepository

        path = database_path()
        with LeadStore(path, initialize=False) as store, rate_limit_scope(path):
            engine = SimpleNamespace(store=store, connection=store.connection)
            try:
                if payload.mission_id == "beings":
                    from .summaries import company_summary
                    packet, _ = company_summary(engine, candidate_id)
                else:
                    from ..missions.reads import summary
                    packet = summary(engine, payload.mission_id, candidate_id)
                repo = SalesEngineRepository(store.connection)
                repo.initialize_schema()
                return find_contacts(repo, packet, payload.person_key,
                                     confirmed_maximum_microusd=payload.confirmed_maximum_microusd,
                                     retry_cache_key=payload.retry_cache_key)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, "AI contact lookup failed. No automatic retry was sent; "
                                    "any unconfirmed charge remains reserved.") from exc

    @router.post("/companies/{candidate_id}/contacts/find-phone")
    def contact_phone_lookup(candidate_id: int, payload: WorkPhoneLookup):
        from ..provider_rate_limit import rate_limit_scope
        from .persistence import SalesEngineRepository
        from .phone_lookup import find_phone

        path = database_path()
        with LeadStore(path, initialize=False) as store, rate_limit_scope(path):
            engine = SimpleNamespace(store=store, connection=store.connection)
            try:
                if payload.mission_id == "beings":
                    from .summaries import company_summary

                    packet, _ = company_summary(engine, candidate_id)
                else:
                    from ..missions.reads import summary

                    packet = summary(engine, payload.mission_id, candidate_id)
                return asyncio.run(find_phone(
                    SalesEngineRepository(store.connection), packet, payload.person_key,
                    payload.confirmed_maximum_microusd, manual_retry=payload.manual_retry,
                ))
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    502, "Phone lookup failed. Check the provider status before retrying."
                ) from exc

    @router.post("/outbound/manual")
    def outbound_manual(payload: OutboundManual):
        from .outbound import save_manual
        with LeadStore(database_path(), initialize=False) as store:
            try:
                return save_manual(store.connection, **payload.model_dump())
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/outbound/{person_id}")
    def outbound_detail_read(person_id: int):
        return outbound_details_access(person_id)

    @router.put("/outbound/{person_id}/details")
    def outbound_detail_write(person_id: int, payload: OutboundDetails):
        return outbound_details_access(person_id, payload.model_dump())

    def outbound_details_access(person_id, details=None):
        from .outbound import person_details
        with LeadStore(database_path(), initialize=False, read_only=details is None) as store:
            try:
                return person_details(store.connection, person_id, details)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/outbound/{person_id}/note")
    def outbound_note_read(person_id: int):
        return outbound_note_access(person_id)

    @router.put("/outbound/{person_id}/note")
    def outbound_note_write(person_id: int, payload: OutboundNote):
        return outbound_note_access(person_id, payload.note)

    def outbound_note_access(person_id, note=None):
        from .outbound import person_note
        with LeadStore(database_path(), initialize=False) as store:
            try:
                return person_note(store.connection, person_id, note)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.put("/outbound/{person_id}/contact-method")
    def outbound_contact_method(person_id: int, payload: OutboundContactMethod):
        from .outbound import set_contact_method
        with LeadStore(database_path(), initialize=False) as store:
            try:
                return set_contact_method(store.connection, person_id, payload.contact_method)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/outbound/{person_id}/touchpoints")
    def outbound_touchpoint(person_id: int, payload: OutboundTouchpoint):
        from .outbound import set_contact_method
        with LeadStore(database_path(), initialize=False) as store:
            try:
                return set_contact_method(store.connection, person_id,
                                          payload.contact_method, append=True)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.patch("/outbound/{person_id}")
    def outbound_status(person_id: int, payload: OutboundStatus):
        from .outbound import set_status
        with LeadStore(database_path(), initialize=False) as store:
            try:
                return set_status(store.connection, person_id, payload.status)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/buyer-intelligence/quote")
    def buyer_intelligence_quote(payload: BuyerCollectionQuote):
        from ..buyer_intelligence_collection import create_quote

        with LeadStore(database_path()) as store:
            try:
                return create_quote(store, **payload.model_dump())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/buyer-intelligence/status")
    def latest_buyer_intelligence_status():
        from ..buyer_intelligence_collection import quote_status

        with LeadStore(database_path()) as store:
            row = store.connection.execute(
                "SELECT id FROM buyer_collection_quotes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return quote_status(store.connection, row[0]) if row else {"status": "idle"}

    @router.post("/buyer-intelligence/{quote_id}/cancel")
    def cancel_buyer_intelligence(quote_id: str):
        from ..buyer_intelligence_collection import quote_status

        with LeadStore(database_path()) as store:
            store.connection.execute(
                "UPDATE buyer_collection_quotes SET status='cancel_requested' "
                "WHERE id=? AND status='running'",
                (quote_id,),
            )
            store.connection.commit()
            try:
                return quote_status(store.connection, quote_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.get("/buyer-intelligence/{quote_id}")
    def buyer_intelligence_status(quote_id: str):
        from ..buyer_intelligence_collection import quote_status

        with LeadStore(database_path()) as store:
            try:
                return quote_status(store.connection, quote_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.post("/buyer-intelligence/{quote_id}/start")
    def buyer_intelligence_start(
        quote_id: str, payload: BuyerCollectionStart, background: BackgroundTasks
    ):
        from datetime import UTC, datetime

        from ..buyer_intelligence_collection import _claim_quote, quote_status, run_quote

        with LeadStore(database_path()) as store:
            try:
                plan = _claim_quote(
                    store, quote_id, payload.confirmed_maximum_microusd, datetime.now(UTC)
                )
            except (ValueError, LookupError) as exc:
                raise HTTPException(409, str(exc)) from exc
            result = quote_status(store.connection, quote_id)
        background.add_task(
            run_quote,
            database_path(),
            quote_id,
            payload.confirmed_maximum_microusd,
            prepared_plan=plan,
        )
        return result

    @router.get("/companies/{candidate_id}/emails/estimate")
    def company_emails_estimate(candidate_id: int):
        try:
            database = database_path()
            estimate = company_email_estimate(database, candidate_id)
            if os.getenv("MICROLINK_API_KEY", "").strip():
                with LeadStore(database, initialize=False, read_only=True) as store:
                    target = company_contact_crawl_target(store, candidate_id, datetime.now(UTC))
                estimate["microlink_max_requests"] = REQUESTS_PER_COMPANY if target else 0
                if (resolve_treg_credentials() is None
                        or estimate["people_checked_without_candidates"]):
                    estimate.update(maximum_microusd=0, needs_people_scan=False)
            return estimate
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/companies/{candidate_id}/emails")
    async def company_emails(candidate_id: int, payload: EmailLookupRequest):
        credentials = resolve_treg_credentials()
        microlink_key = os.getenv("MICROLINK_API_KEY", "").strip()
        crawl = payload.confirmed_microlink_requests == REQUESTS_PER_COMPANY
        if payload.confirmed_microlink_requests and (not crawl or not microlink_key):
            raise HTTPException(422, "Refresh the Microlink contact estimate")
        if payload.confirmed_maximum_microusd and credentials is None:
            raise HTTPException(422, "Treg is not configured")
        if not (payload.confirmed_maximum_microusd or crawl):
            raise HTTPException(422, "No contact provider request was confirmed")
        database = database_path()
        try:
            if payload.confirmed_maximum_microusd:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(45.0, connect=10.0),
                    follow_redirects=False,
                    trust_env=False,
                ) as http_client:
                    result = await find_company_emails(
                        database, candidate_id, TregPeopleClient(credentials, client=http_client),
                        confirmed_maximum_microusd=payload.confirmed_maximum_microusd,
                    )
            else:
                company_email_estimate(database, candidate_id)
                result = {"attempted": 0, "actual_cost_usd": 0, "message": ""}
            if crawl:
                microlink = await crawl_company_contacts(database, candidate_id, microlink_key)
                result["microlink_contact_search"] = microlink
                result["message"] = " ".join(filter(None, (
                    result.get("message"),
                    {
                        "complete": "Microlink contact search complete.",
                        "partial": "Microlink contact search finished with gaps.",
                        "failed": "Microlink contact search failed.",
                        "skipped": "Microlink contact search is already current or unavailable.",
                    }[microlink["status"]],
                )))
            return result
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @contextmanager
    def service():
        with LeadStore(database_path()) as store:
            try:
                yield SalesEngineService(store)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    from .read_pool import ReadPool, run_read

    pool = ReadPool()
    router.add_event_handler("shutdown", pool.close)

    async def read(request, operation):
        try:
            return await run_read(request, pool, database_path(), operation)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/missions")
    async def missions(request: Request):
        from ..missions.store import list_missions

        return await read(
            request, lambda engine: _conditional_response(request, list_missions(engine.connection))
        )

    @router.get("/missions/{mission_id}/export.csv")
    def mission_export(
        mission_id: str, search: str = Query("", max_length=200), saved: bool = False
    ):
        from ..missions.reads import export_csv

        if mission_id == "beings":
            raise HTTPException(400, "Use the existing Beings records export")
        with service() as engine:
            try:
                content = export_csv(engine, mission_id, search=search, saved=saved)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
        return Response(
            content,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="mission-companies.csv"'},
        )

    @router.post("/missions")
    def new_mission(
        payload: MissionCreate, background: BackgroundTasks, request: Request, response: Response,
    ):
        from ..missions.compiler import compile_mission
        from ..missions.scoring import refresh
        from ..missions.store import create_mission

        with _mission_request(request, response, "create") as progress:
            result = None
            try:
                with mission_write_lock, service() as engine:
                    progress["stage"] = "save"
                    result = create_mission(
                        engine.connection,
                        payload.name,
                        payload.brief,
                        pending_ai=payload.compile_with_ai,
                        setup=payload.setup,
                        compiler_model=payload.compiler_model,
                    )
                    if payload.compile_with_ai and not payload.defer_ai:
                        progress["stage"] = "compile"
                        result = compile_mission(
                            engine.store, result["id"], model=payload.compiler_model
                        )
            except sqlite3.OperationalError as exc:
                raise _mission_write_busy(exc, saved=result is not None) from exc
            compilation = result.get("compilation") or {}
            progress.update(
                compilation_status=compilation.get("status"),
                compiler_error=compilation.get("error_code"),
            )

        from ..desktop_ranking.server import configured_socket

        def local_refresh():
            with LeadStore(database_path()) as store:
                refresh(store, mission_id=result["id"])

        if (
            not result["spec"].get("compiler_pending")
            and configured_socket(database_path()) is None
        ):
            background.add_task(local_refresh)
        return result

    @router.post("/missions/{mission_id}/compile")
    def mission_compile(
        mission_id: str, payload: MissionCompile, request: Request, response: Response,
    ):
        from ..missions.compiler import compile_mission

        with _mission_request(request, response, "compile") as progress:
            try:
                with mission_write_lock, service() as engine:
                    progress["stage"] = "compile"
                    result = compile_mission(
                        engine.store, mission_id, model=payload.compiler_model,
                        retry_cache_key=payload.retry_cache_key,
                    )
            except sqlite3.OperationalError as exc:
                raise _mission_write_busy(exc, saved=True) from exc
            compilation = result.get("compilation") or {}
            progress.update(
                compilation_status=compilation.get("status"),
                compiler_error=compilation.get("error_code"),
            )
            return result

    @router.patch("/missions/{mission_id}")
    def mission_update(mission_id: str, payload: MissionUpdate):
        from ..missions.store import update_mission

        with service() as engine:
            try:
                return update_mission(
                    engine.connection, mission_id, name=payload.name, archived=payload.archived
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/missions/{mission_id}/run")
    def mission_run(mission_id: str):
        from ..missions.store import run_mission

        with service() as engine:
            try:
                return run_mission(engine.connection, mission_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

    @router.delete("/missions/{mission_id}")
    def mission_delete(mission_id: str):
        from ..missions.store import delete_mission

        with service() as engine:
            try:
                return delete_mission(engine.connection, mission_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @router.get("/missions/{mission_id}/companies/{candidate_id}/review-quote")
    def mission_review_quote(mission_id: str, candidate_id: int):
        from ..missions.review import quote

        with service() as engine:
            try:
                return quote(engine.store, mission_id, candidate_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.post("/missions/{mission_id}/companies/{candidate_id}/review")
    def mission_review(mission_id: str, candidate_id: int, payload: MissionReviewStart):
        from ..missions.review import run

        with service() as engine:
            try:
                return run(engine.store, mission_id, candidate_id, **payload.model_dump())
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @router.get("/overview")
    async def overview(
        request: Request,
        search: str = Query("", max_length=200),
        tag: str = Query("", max_length=40),
        limit: int = Query(30, ge=1, le=250),
        prospects_only: bool = False,
        compact: bool = False,
        mode: RankingMode = "balanced",
        offset: int = Query(0, ge=0),
        mission_id: str = Query("beings", max_length=64),
        sort: Literal["ranked", "newest"] = "ranked",
    ):
        if sort == "newest":
            from ..missions.reads import newest_companies

            return await read(
                request,
                lambda engine: _conditional_response(
                    request,
                    newest_companies(
                        engine, mission_id, search=search, tag=tag, limit=limit, offset=offset,
                        prospects_only=prospects_only, mode=mode,
                    ),
                ),
            )
        if mission_id != "beings":
            from ..missions.reads import list_companies

            return await read(
                request,
                lambda engine: _conditional_response(
                    request,
                    list_companies(engine, mission_id, search=search, tag=tag,
                                   limit=limit, offset=offset),
                ),
            )
        if not compact:

            def legacy():
                with service() as engine:
                    return engine.queue(
                        search=search, tag=tag,
                        limit=limit,
                        prospects_only=prospects_only,
                        mode=mode,
                        offset=offset,
                    )

            return await asyncio.to_thread(legacy)
        return await read(
            request,
            lambda engine: _conditional_response(
                request,
                engine.queue(
                    search=search, tag=tag,
                    limit=limit,
                    prospects_only=prospects_only,
                    compact=True,
                    mode=mode,
                    offset=offset,
                ),
            ),
        )

    @router.get("/pipeline")
    async def pipeline(
        request: Request,
        search: str = Query("", max_length=200),
        tag: str = Query("", max_length=40),
        stage: str = Query("", max_length=30),
        limit: int = Query(30, ge=1, le=250),
        offset: int = Query(0, ge=0),
        compact: bool = False,
        mode: RankingMode = "balanced",
        mission_id: str = Query("beings", max_length=64),
    ):
        if mission_id != "beings":
            from ..missions.reads import list_companies

            return await read(
                request,
                lambda engine: _conditional_response(
                    request,
                    list_companies(
                        engine,
                        mission_id,
                        search=search, tag=tag,
                        limit=limit,
                        offset=offset,
                        saved=True,
                        stage=stage,
                    ),
                ),
            )
        if not compact:

            def legacy():
                with service() as engine:
                    return engine.pipeline(
                        search=search, tag=tag, stage=stage, limit=limit, offset=offset, mode=mode
                    )

            return await asyncio.to_thread(legacy)
        return await read(
            request,
            lambda engine: _conditional_response(
                request,
                engine.pipeline(
                    search=search, tag=tag, stage=stage, limit=limit, offset=offset,
                    compact=True, mode=mode
                ),
            ),
        )

    @router.get("/tools")
    def tools(request: Request):
        with service() as engine:
            return _conditional_response(request, engine.tools())

    @router.get("/pipeline/{account_id}/summary")
    async def pipeline_summary(
        account_id: int,
        request: Request,
        mode: RankingMode = "balanced",
        mission_id: str = Query("beings", max_length=64),
    ):
        if mission_id != "beings":
            from ..missions.reads import summary

            def mission_response(engine):
                return tagged_response(
                    request, engine, summary(engine, mission_id, account_id=account_id)
                )

            return await read(request, mission_response)

        def response(engine):
            payload, _ = engine.pipeline_summary(account_id, mode=mode)
            return tagged_response(request, engine, payload)

        return await read(request, response)

    @router.get("/pipeline/{account_id}")
    def pipeline_company(account_id: int, mode: RankingMode = "balanced"):
        with service() as engine:
            return engine.pipeline_company(account_id, mode=mode)

    @router.post("/companies/{candidate_id}/deal")
    def open_deal(candidate_id: int):
        with service() as engine:
            return engine.open_deal(candidate_id)

    @router.get("/companies/{candidate_id}")
    def company(candidate_id: int, mode: RankingMode = "balanced"):
        with service() as engine:
            return engine.packet(candidate_id, mode=mode)

    @router.get("/companies/{candidate_id}/summary")
    async def company_summary(
        candidate_id: int,
        request: Request,
        mode: RankingMode = "balanced",
        mission_id: str = Query("beings", max_length=64),
    ):
        if mission_id != "beings":
            from ..missions.reads import summary

            return await read(
                request,
                lambda engine: tagged_response(
                    request, engine, summary(engine, mission_id, candidate_id)
                ),
            )

        def response(engine):
            payload, _ = engine.company_summary(candidate_id, mode=mode)
            return tagged_response(request, engine, payload)

        return await read(request, response)

    @router.get("/companies/{candidate_id}/export")
    def export(candidate_id: int, format: Literal["json", "markdown"] = "markdown"):
        with service() as engine:
            packet = engine.packet(candidate_id)
            if format == "json":
                return Response(
                    json.dumps(packet, indent=2),
                    media_type="application/json",
                    headers={
                        "Content-Disposition": f'attachment; filename="sales-{candidate_id}.json"'
                    },
                )
            lines = [
                f"# {packet['company_name']} — saved records",
                "",
                f"Company: https://{packet['company_key']}",
                f"Sales priority: {packet['sales_priority']['score']}",
                f"Evidence observed: {packet['observed_at']}",
                f"Prepared: {packet['as_of']}",
                f"Readiness: {packet['buyer_assessment']['readiness']}",
                "",
            ]
            for criterion in packet["buyer_assessment"]["criteria"]:
                lines.append(f"- {criterion['label']}: {criterion['state']}")
                for evidence in criterion["evidence"]:
                    lines.extend(
                        [
                            evidence["note"],
                            str(evidence["observed_at"]),
                            "Source: "
                            + evidence["source_kind"]
                            + " · Recorded by: "
                            + (evidence.get("recorded_by") or "Not recorded"),
                            evidence.get("source_url") or "Private record",
                            "",
                        ]
                    )
                    if evidence.get("fact_id") is not None:
                        lines.append("Buyer fact: " + str(evidence["fact_id"]))
            lines.extend(["", "## Source records", ""])
            for event in packet["workload"].get("source_events", []):
                lines.extend(
                    [
                        event.get("title", ""),
                        event.get("excerpt", ""),
                        event.get("source_url", ""),
                        str(event.get("observed_at", "")),
                        "",
                    ]
                )
            opportunities = [item for item in packet["opportunities"] if item["status"] != "closed"]
            for item in opportunities:
                lines.extend(
                    [
                        f"## {item['title']}",
                        "",
                        f"Status: {item['status']}",
                        f"Approval owner: {item['approval_owner'] or 'Unknown'}",
                        "Buyer-estimated monthly workload: "
                        + (
                            f"{item['estimated_hours']} hours"
                            if item["estimated_hours"] is not None
                            else "Unknown"
                        ),
                        "",
                    ]
                )
                for evidence in item["evidence"]:
                    lines.extend(
                        [
                            f"> {evidence['excerpt']}",
                            evidence.get("source_url")
                            or "Private buyer conversation; no public URL",
                            f"{evidence['evidence_kind']} · {evidence['observed_at']}",
                            "Source: "
                            + evidence.get("source_kind", "public_source")
                            + " · Recorded by: "
                            + (evidence.get("recorded_by") or "Not recorded"),
                            "",
                        ]
                    )
            if not opportunities:
                lines.append("No open production opportunity is recorded.")
            for review in packet.get("reviews", []):
                lines.extend(
                    [
                        "",
                        "## Research note",
                        review.get("notes", ""),
                        str(review.get("observed_at", "")),
                    ]
                )
            return Response(
                "\n".join(lines),
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="sales-{candidate_id}.md"'},
            )

    @router.post("/companies/{candidate_id}/buyer-facts")
    def buyer_fact(candidate_id: int, payload: BuyerFact):
        with service() as engine:
            return engine.save_buyer_fact(candidate_id, payload)

    @router.post("/opportunities")
    def opportunity(payload: ProductionOpportunity):
        with service() as engine:
            return engine.repo.save_opportunity(payload)

    @router.post("/relationships")
    def relationship(payload: RelationshipEvidence):
        with service() as engine:
            return engine.repo.save_relationship(payload)

    @router.post("/reviews")
    def review(payload: ResearchReview):
        with service() as engine:
            return engine.repo.add_review(payload)

    @router.get("/learning")
    def learning():
        with service() as engine:
            return engine.repo.learning_report()

    @router.post("/budget")
    def budget(payload: BudgetPolicy):
        with service() as engine:
            return engine.repo.configure_budget(payload)

    @router.post("/companies/{candidate_id}/decision")
    def decision(candidate_id: int, payload: DecisionRequest):
        with service() as engine:
            return engine.record_decision(candidate_id, payload.idempotency_key)

    @router.post("/companies/{candidate_id}/watch")
    def watch(candidate_id: int, payload: WatchRequest):
        with service() as engine:
            return engine.watch(candidate_id, active=payload.active)

    @router.post("/companies/{candidate_id}/events")
    def event(candidate_id: int, payload: WorkloadEvent):
        with service() as engine:
            return engine.add_workload_input(candidate_id, payload)

    @router.post("/companies/{candidate_id}/windows")
    def window(candidate_id: int, payload: CreativeWindow):
        with service() as engine:
            return engine.add_workload_input(candidate_id, payload)

    @router.post("/companies/{candidate_id}/annotations")
    def annotation(candidate_id: int, payload: CreativeAnnotation):
        with service() as engine:
            return engine.save_annotation(candidate_id, payload)

    @router.post("/companies/{candidate_id}/benchmark")
    def benchmark(candidate_id: int, payload: BenchmarkRequest):
        with service() as engine:
            return engine.save_concept_labels(candidate_id, payload.human_concept_labels)

    @router.get("/visual/quote")
    def visual_quote():
        return {
            "model": MODEL,
            "maximum_microusd": MAXIMUM_MICROUSD,
            "maximum_image_dimension": 512,
            "automatic_retries": 0,
        }

    @router.post("/companies/{candidate_id}/visual")
    def visual(candidate_id: int, payload: VisualRequest):
        with service() as engine:
            _, key, _ = engine._company(candidate_id)
            try:
                result = analyze_visual(engine.repo, company_key=key, **payload.model_dump())
            except (ValueError, json.JSONDecodeError):
                raise
            except Exception as exc:
                raise HTTPException(
                    502,
                    "Image analysis did not complete. The cost reservation is retained; "
                    "no automatic retry was sent.",
                ) from exc
            engine.save_annotation(
                candidate_id, CreativeAnnotation.model_validate(result["annotation"])
            )
            return result

    @router.post("/refresh")
    def refresh(limit: int = Query(50, ge=1, le=100)):
        with service() as engine:
            return refresh_rankings(engine.store, limit=limit)

    @router.get("/research-yield")
    def research_yield():
        with service() as engine:
            return {
                "methods": empirical_scanner_yield(engine.store),
                "basis": "Selected historical populations; operational yield, not buyer conversion",
            }

    return router
