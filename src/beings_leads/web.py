from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import sys
import tempfile
import uuid
import webbrowser
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ExitStack, asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ad_discovery_service import (
    AdDiscoveryExecutor,
    PaidBudgetExhausted,
    PaidRunCancelled,
    _microusd,
)
from .ad_intelligence import (
    AdDiscoveryRequest,
    AdScanQuote,
    default_rate_cards,
    estimate_ad_scan,
)
from .agency_discovery import discover_agencies
from .ai_source_queries import MAXIMUM as AI_QUERY_SCOUT_MAXIMUM
from .ai_source_queries import scout_ai_query_sources
from .company_import import MAX_IMPORT_BYTES, save_company_urls
from .company_purge import (
    ProtectedEndings,
    PurgeCancellation,
    PurgeCancelled,
    PurgeConfirmation,
    PurgeCriteria,
    confirm_purge,
    preview_purge,
    purge_settings,
    reclaim_purge_storage,
    save_protected_endings,
)
from .config import USER_AGENT
from .creative_capacity import (
    CreativeCapacityMap,
    DeliveryOutcome,
    DiscoveryReview,
    EvidenceClaim,
    SalesOutcome,
    StrategicQualification,
)
from .dataforseo_intelligence import (
    BULK_LIMIT as DATAFORSEO_BULK_BATCH_LIMIT,
)
from .dataforseo_intelligence import (
    REFRESH_DAYS as DATAFORSEO_REFRESH_DAYS,
)
from .dataforseo_intelligence import (
    DataForSEOClient,
    DataForSEOError,
    group_seo_cohort,
    project_dataforseo_item,
    seo_registered_domain,
)
from .desktop_ranking.control import (
    DesktopRankingUnavailable,
    reproject_candidates,
    require_connected_owner,
)
from .discovery import DiscoveryError, discover_apex, normalize_apex
from .domain_policy import disallowed_domains
from .free_source_scout import scout_free_sources
from .frontier_intelligence import SCANNER_DEFINITIONS
from .inspect import _bounded_get
from .inventory_census import CENSUS
from .inventory_check import InventoryCheck
from .market_intelligence import (
    MarketFeedLimits,
    MarketFetchResponse,
    MarketPayloadError,
    poll_market_source,
    require_market_source_policy,
)
from .market_projection import project_market_batch
from .microlink_contacts_scan import MicrolinkContactScan
from .microlink_hydration import MicrolinkHydrationOptions
from .microlink_scan import MicrolinkScan
from .models import (
    FRONTIER_SCANNER_IDS,
    DiscoveredHost,
    ScannerExecutionSummary,
    SourceSuggestion,
)
from .provider_cooldown import (
    GDELT_HOST,
    active_provider_cooldown,
    record_provider_cooldown,
    retry_after_timestamp,
)
from .provider_rate_limit import DATABASE, rate_limit_scope
from .rehydration import REHYDRATION_DAYS
from .research import build_research_brief
from .rss_discovery import RSS_SOURCES, RSSFetcher, collect_rss_source, save_rss_candidates
from .sales_engine.api import create_router as create_sales_engine_router
from .scan_activity import decode_scan_action
from .scan_diagnostics import CHILD_ENV, MARKER, SCAN_ID, SOURCE, source_time, source_trace
from .scan_diagnostics import emit as emit_scan_diagnostic
from .scan_diagnostics import error_fields as scan_diagnostic_error
from .scanner_manifest import validate_manifest
from .site_research import DEFAULT_SITE_RESEARCH_PAGES
from .smart_scan_audit import measure_operation
from .source_crawl_cache import SourceCrawlCache
from .source_discovery import (
    SOURCE_MICROLINK_MAX_PAGES,
    SourceDiscoveryLimits,
    discover_source,
    discovery_method_for,
    source_execution_blockers,
)
from .source_registry import append_registry_source, parse_registry
from .source_scout import DEFAULT_KEYWORD_COMBINATIONS, scout_sources
from .source_scout import source_scout_output_coverage as _source_scout_output_coverage
from .store import SOURCE_EXECUTION_COUNTER_FIELDS, AccountConflictError, LeadStore
from .treg_people import (
    CHAMPION_TITLES,
    DEFAULT_EMAIL_MAX_COST_USD,
    DEFAULT_PEOPLE_LIMIT,
    ECONOMIC_TITLES,
    TregPeopleClient,
    TregPeopleError,
    TregUnsettledError,
    estimate_people_hydration,
    project_people_result,
    resolve_treg_credentials,
    select_people,
)
from .treg_people import (
    EXTRACTOR_VERSION as TREG_PEOPLE_EXTRACTOR_VERSION,
)
from .treg_people import (
    MAX_COHORT_SIZE as TREG_PEOPLE_MAX_COHORT_SIZE,
)
from .treg_people import (
    MAX_PEOPLE_LIMIT as TREG_PEOPLE_MAX_PEOPLE_LIMIT,
)
from .treg_people import (
    MIN_GOOD_CONTACTS as TREG_PEOPLE_MIN_GOOD_CONTACTS,
)
from .treg_people import (
    REFRESH_DAYS as TREG_PEOPLE_REFRESH_DAYS,
)
from .treg_people import (
    SCANNER_ID as TREG_PEOPLE_SCANNER_ID,
)
from .treg_people import (
    SCANNER_VERSION as TREG_PEOPLE_SCANNER_VERSION,
)

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
DEFAULT_DB = ROOT / "data" / "leads.db"
REGISTRY_PATH = ROOT / "docs" / "validated-crt-prospecting-domains.md"
REPORT_PATH = ROOT / "reports" / "latest.md"
JSON_REPORT_PATH = ROOT / "reports" / "latest.json"
ENV_PATH = ROOT / ".env"
MANAGED_SECRETS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "APIFY_API_TOKEN",
        "MICROLINK_API_KEY",
        "DATAFORSEO_LOGIN",
        "DATAFORSEO_PASSWORD",
        "TREG_TOKEN",
        "TIKTOK_COMMERCIAL_ACCESS_TOKEN",
    }
)
OPENAI_SECRET = "OPENAI_API_KEY"
OPENAI_ADMIN_SECRET = "OPENAI_ADMIN_KEY"
APIFY_SECRET = "APIFY_API_TOKEN"
MICROLINK_SECRET = "MICROLINK_API_KEY"
DATAFORSEO_LOGIN_SECRET = "DATAFORSEO_LOGIN"
DATAFORSEO_PASSWORD_SECRET = "DATAFORSEO_PASSWORD"
STAGES = {"research", "qualified", "contacted", "replied", "meeting", "won", "lost"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
logger = logging.getLogger(__name__)
SCAN_STARTUP_GRACE_SECONDS = 30
SCAN_STALL_SECONDS = 180
SCAN_RETRY_BACKOFF_SECONDS = 5
MAX_SCAN_RETRIES = 2
SOURCE_SCOUT_TIMEOUT_SECONDS = 600  # Two bounded 300-second provider attempts.
DEFAULT_SCOUT_SUGGESTIONS = 12
DEFAULT_SCOUT_TOOL_CALLS = 10
DEFAULT_SCOUT_KEYWORD_COMBINATIONS = DEFAULT_KEYWORD_COMBINATIONS
MEGA_MAX_HOSTS_PER_SOURCE = 10_000
MEGA_MAX_NETWORK_INSPECTIONS = 1_000
MEGA_MAX_AI_CALLS = 12
MEGA_MAX_AI_WEB_CALLS = 8
AUTO_SOURCE_MAX_PAGES = 50
AUTO_SOURCE_MAX_CANDIDATES = 500
AUTO_SOURCE_MAX_NETWORK_INSPECTIONS = 500
AUTO_SOURCE_MAX_AI_TOOL_CALLS = 10
SOURCE_METHOD_FRESH_HOURS = 24
DIRECTORY_RESCAN_DAYS = 90
SMART_SCAN_LOG_LIMIT = 120
SMART_SCAN_BROWSER_LOG_LIMIT = 80
SMART_SCAN_ACTION_LOG_LIMIT = 5_000
SMART_SCAN_HYDRATION_BATCH_SIZE = 1_000
SMART_SCAN_SCANNER_BATCH_SIZE = 25
SMART_SCAN_RANKING_BATCH_SIZE = 25
SMART_SCAN_SOURCE_AI_TOOL_CALLS = 10
SMART_SCAN_SCOUT_AI_TOOL_CALLS = 6
SMART_SCAN_CANDIDATE_AI_CALLS = 100
SMART_SCAN_CANDIDATE_AI_WEB_CALLS = 8
SMART_SCAN_MAX_CANDIDATE_AI_CALLS = 100
SMART_SCAN_SOURCE_AI_ESTIMATE_PER_TOOL_CALL_USD = Decimal("0.02")
SMART_SCAN_CANDIDATE_AI_ESTIMATE_PER_SITE_USD = Decimal("0.011")
SMART_SCAN_CANDIDATE_AI_WEB_ESTIMATE_PER_SITE_USD = Decimal("0.02")
SMART_SCAN_MAX_FRONTIER_SCANS = 500
SMART_SCAN_DATAFORSEO_DEFAULT_TARGET_LIMIT = 100
SMART_SCAN_DATAFORSEO_MAX_TARGET_LIMIT = 100_000
SMART_SCAN_DATAFORSEO_DETAIL_DIVISOR = 20
SMART_SCAN_DATAFORSEO_DETAIL_CONCURRENCY = 10
SMART_SCAN_DATAFORSEO_BULK_TASK_ESTIMATE_USD = Decimal("0.012")
SMART_SCAN_DATAFORSEO_BULK_ITEM_ESTIMATE_USD = Decimal("0.00012")
SMART_SCAN_DATAFORSEO_DETAIL_TASK_ESTIMATE_USD = Decimal("0.01212")
SMART_SCAN_CONNECTIVITY_PROBE_SECONDS = 15
SMART_SCAN_RECONNECT_RETRY_SECONDS = 5
SMART_SCAN_SLEEP_GAP_SECONDS = 30
SMART_SCAN_CONNECTIVITY_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 53),
    ("9.9.9.9", 53),
    ("example.com", 443),
)
MAX_ACCOUNT_IMPORT_ROWS = 200
MAX_ACCOUNT_IMPORT_BYTES = 1_000_000
AD_QUOTE_TTL_MINUTES = 20
PEOPLE_HYDRATION_QUOTE_TTL_MINUTES = 20
AD_RATE_CARD_VERSION = "apify-store-2026-08-27-v1"
DISCOVERY_METHOD_TABS: tuple[dict[str, object], ...] = (
    {
        "id": "web_ct",
        "label": "Web & CT",
        "description": "Enumerate enabled tenant apexes and hydrate public company websites.",
        "source_kinds": [],
        "requires": [],
    },
    {
        "id": "company_intelligence",
        "label": "Company intelligence",
        "description": (
            "Manually run any registered company-detail scanner against the strongest "
            "current targets without bypassing its 90-day reuse floor."
        ),
        "source_kinds": [],
        "requires": [],
    },
    {
        "id": "tenant_footprints",
        "label": "Tenant footprints",
        "description": (
            "Enumerate public tenant domains, showcases, and customer stories with a "
            "private-suffix fallback."
        ),
        "source_kinds": ["tenant_apex"],
        "requires": [],
    },
    {
        "id": "directories",
        "label": "Directories",
        "description": (
            "Crawl public agency and brand directories, nested profiles, and roster PDFs."
        ),
        "source_kinds": ["agency_directory", "brand_directory"],
        "requires": [],
    },
    {
        "id": "portfolios",
        "label": "Portfolios",
        "description": "Traverse parent-authored brand portfolios into operating company domains.",
        "source_kinds": ["brand_group_portfolio"],
        "requires": [],
    },
    {
        "id": "customer_lists",
        "label": "Customer lists",
        "description": (
            "Enumerate public platform customers, partners, marketplaces, and case studies."
        ),
        "source_kinds": ["platform_customer_list"],
        "requires": [],
    },
    {
        "id": "searches",
        "label": "Searches",
        "description": "Run accepted repeatable search recipes with cited official-domain results.",
        "source_kinds": ["repeatable_search"],
        "requires": [OPENAI_SECRET],
    },
    {
        "id": "public_graph",
        "label": "Public graph",
        "description": "Rotate through cached OpenStreetMap and Wikidata agency ecosystems.",
        "source_kinds": [],
        "requires": [],
    },
    {
        "id": "paid_ads",
        "label": "Paid ads",
        "description": "Discover and hydrate advertisers after an exact provider-cap approval.",
        "source_kinds": [],
        "requires": [APIFY_SECRET],
    },
    {
        "id": "find_sources",
        "label": "Find sources",
        "description": "Scout, review, and route new public source methods into executable tabs.",
        "source_kinds": [],
        "requires": [OPENAI_SECRET],
    },
)
EXECUTABLE_DISCOVERY_METHODS = frozenset(
    {
        "tenant_footprints",
        "directories",
        "portfolios",
        "customer_lists",
        "searches",
        "public_graph",
        "company_intelligence",
    }
)

SERVER_OWNED_DISCOVERY_METHODS = frozenset(
    {"public_graph", "company_intelligence"}
)
RETIRED_LEGAL_DISCOVERY_METHOD = "legal_filings"
SOURCE_CRAWL_DISCOVERY_METHODS = frozenset(
    {"tenant_footprints", "directories", "portfolios", "customer_lists"}
)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
    logger.propagate = False


def database_path() -> Path:
    return Path(os.getenv("BEINGS_LEADS_DB", str(DEFAULT_DB))).resolve()


def _env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_env_file() -> None:
    """Load simple .env entries without printing or exposing their values."""
    if not ENV_PATH.is_file():
        return
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw_value = stripped.partition("=")
        secret_name = name.strip()
        if secret_name in MANAGED_SECRETS and not os.getenv(secret_name):
            value = _env_value(raw_value)
            if value:
                os.environ[secret_name] = value


def _validate_secret(value: str) -> str:
    value = value.strip()
    invalid = any(char in value for char in "\r\n\x00") or any(char.isspace() for char in value)
    if not value or invalid:
        raise HTTPException(status_code=422, detail="Enter a non-empty API key without whitespace")
    return value


def _write_secrets(updates: dict[str, str | None]) -> None:
    """Update only managed env entries and replace the file atomically once."""

    if not updates or set(updates) - MANAGED_SECRETS:
        raise ValueError("Unsupported managed secret update")
    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    lines = existing.splitlines(keepends=True)
    replaced: set[str] = set()
    output: list[str] = []
    for line in lines:
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        if content.endswith("\r"):
            content = content[:-1]
            newline = "\r\n"
        stripped = content.strip()
        matched_name = next(
            (name for name in updates if stripped.startswith(f"{name}=")),
            None,
        )
        if matched_name is not None:
            value = updates[matched_name]
            if value is not None and matched_name not in replaced:
                output.append(f"{matched_name}={value}{newline or chr(10)}")
                replaced.add(matched_name)
            continue
        output.append(line)
    for name, value in updates.items():
        if value is None or name in replaced:
            continue
        if output and not output[-1].endswith(("\n", "\r")):
            output.append("\n")
        output.append(f"{name}={value}\n")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".env.", dir=ENV_PATH.parent)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.writelines(output)
        os.replace(temp_name, ENV_PATH)
        os.chmod(ENV_PATH, 0o600)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            os.unlink(temp_name)
        raise


def _write_secret(name: str, value: str | None) -> None:
    """Update one managed env entry through the atomic multi-secret writer."""

    if name not in MANAGED_SECRETS:
        raise ValueError(f"Unsupported managed secret: {name}")
    _write_secrets({name: value})


def _secret_metadata(name: str) -> dict[str, object]:
    if name not in MANAGED_SECRETS:
        raise ValueError(f"Unsupported managed secret: {name}")
    return {"configured": bool(os.getenv(name))}


def _dataforseo_configured() -> bool:
    return bool(
        os.getenv(DATAFORSEO_LOGIN_SECRET)
        and os.getenv(DATAFORSEO_PASSWORD_SECRET)
    )


def _dataforseo_metadata() -> dict[str, object]:
    return {"configured": _dataforseo_configured()}


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_loopback(request: Request) -> None:
    if not _is_loopback_request(request):
        logger.warning(
            "rejected non-loopback secret settings request host=%s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Secret settings are available only locally")


_load_env_file()


def sync_registry() -> int:
    entries = parse_registry(REGISTRY_PATH)
    with LeadStore(database_path()) as store:
        return store.sync_registry_sources(entries)


class WorkflowPatch(BaseModel):
    stage: str | None = None
    starred: bool | None = None
    excluded: bool | None = None
    scan_enabled: bool | None = None
    notes: str | None = Field(default=None, max_length=10_000)
    tags: list[str] | None = Field(default=None, max_length=20)
    next_follow_up_at: datetime | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(len(tag.strip()) > 40 for tag in value):
            raise ValueError("Tags must be 40 characters or fewer")
        return value


AccountSegment = Literal[
    "consumer_brand",
    "consumer_app",
    "digital_product",
    "gaming",
    "subscription_business",
    "saas_ai_adjacency",
    "agency_buyer",
    "agency_referral_partner",
    "other",
]
AccountLeadSource = Literal[
    "past_client",
    "founder_investor_network",
    "specialist_referral",
    "targeted_outbound",
    "other",
]
AccountLifecycle = Literal[
    "research",
    "qualified",
    "outreach",
    "discovery",
    "proposal",
    "won",
    "lost",
    "paused",
]
TargetLane = Literal["buyer", "referral_partner", "all"]
TargetScope = Literal["direct", "adjacency", "unclassified", "out_of_scope", "all"]
TargetSort = Literal[
    "priority", "durable_fit", "contact_now", "target_score", "failure_rate"
]


class AccountCreate(BaseModel):
    candidate_id: int | None = Field(default=None, ge=1)
    company: str = Field(default="", max_length=300)
    website: str = Field(default="", max_length=2_048)
    cohort: str = Field(default="prototype-v1", min_length=1, max_length=100)
    relationship_type: Literal["buyer", "referral_partner"] | None = None
    segment: AccountSegment | None = None
    lead_source: AccountLeadSource = "targeted_outbound"
    referrer: str = Field(default="", max_length=500)
    intro_status: Literal["none", "requested", "opted_in", "introduced", "declined"] = "none"
    notes: str = Field(default="", max_length=10_000)

    @field_validator("company", "website", "cohort", "referrer", "notes")
    @classmethod
    def strip_account_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_manual_identity(self) -> AccountCreate:
        if self.candidate_id is None and (not self.company or not self.website):
            raise ValueError("Manual accounts require company and website")
        return self


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str | None = Field(default=None, min_length=1, max_length=300)
    website: str | None = Field(default=None, min_length=1, max_length=2_048)
    cohort: str | None = Field(default=None, min_length=1, max_length=100)
    relationship_type: Literal["buyer", "referral_partner"] | None = None
    segment: AccountSegment | None = None
    lead_source: AccountLeadSource | None = None
    referrer: str | None = Field(default=None, max_length=500)
    intro_status: Literal["none", "requested", "opted_in", "introduced", "declined"] | None = None
    notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("company", "website", "cohort")
    @classmethod
    def strip_required_patch_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be blank")
        return stripped

    @field_validator("referrer", "notes")
    @classmethod
    def strip_optional_patch_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ClaimPatch(BaseModel):
    signals: list[str] | None = None
    gates: list[str] | None = None
    dimensions: list[str] | None = None
    claim: str | None = Field(default=None, min_length=1)
    epistemic_status: Literal["observed", "inferred", "unknown"] | None = None
    truth_state: Literal["met", "not_met", "unknown"] | None = None
    source_kind: (
        Literal[
            "company_site",
            "ad_library",
            "social_feed",
            "career_page",
            "press_release",
            "public_directory",
            "discovery_call",
            "internal_history",
            "warm_introduction",
            "operator_observation",
            "other",
        ]
        | None
    ) = None
    source_url: str | None = Field(default=None, max_length=2_048)
    observed_at: datetime | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    human_verified: bool | None = None
    verified_by: str | None = Field(default=None, max_length=200)
    verified_at: datetime | None = None


class ActivityCreate(BaseModel):
    activity_type: Literal["email_sent", "linkedin_message", "call", "reply", "meeting", "note"]
    summary: str = Field(min_length=1, max_length=2_000)
    contact_email: str = Field(default="", max_length=320)
    occurred_at: datetime | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Activity summary cannot be blank")
        return value

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not EMAIL_RE.fullmatch(value):
            raise ValueError("Enter a valid contact email")
        return value


class SourceCreate(BaseModel):
    apex: str
    category: Literal["Agency", "Ecommerce", "B2B / SaaS", "Other"] = "Other"
    notes: str = Field(default="Added from dashboard", max_length=500)


class SourcePatch(BaseModel):
    enabled: bool


class SuggestionPatch(BaseModel):
    status: Literal["accepted", "rejected", "pending"]
    activate: bool = True


class SourceHydrationSupplementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_network_inspections: int = Field(default=100, ge=1, le=500)
    max_ai_calls: int = Field(default=50, ge=1, le=500)
    max_ai_web_calls: int = Field(default=8, ge=0, le=100)

    @model_validator(mode="after")
    def validate_ai_caps(self) -> SourceHydrationSupplementRequest:
        if self.max_ai_web_calls > self.max_ai_calls:
            raise ValueError("AI web calls cannot exceed total AI calls")
        return self


class SmartScanStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_terms: list[str] = Field(default_factory=list, max_length=5)

    due_hydration_enabled: bool = True
    people_hydration_enabled: bool = False
    people_company_limit: int = Field(default=100, ge=1, le=TREG_PEOPLE_MAX_COHORT_SIZE)
    paid_ads_enabled: bool = False
    paid_ads_company_limit: int = Field(default=100, ge=1, le=500)
    paid_ads_max_usd: Decimal = Field(default=Decimal("5.00"), ge=Decimal("0.10"),
                                     le=Decimal("100.00"), decimal_places=2)


    ai_enabled: bool = False
    source_ai_enabled: bool | None = None
    microlink_enabled: bool | None = None
    microlink_hydration: MicrolinkHydrationOptions = Field(
        default_factory=MicrolinkHydrationOptions)
    ai_site_limit: int = Field(
        default=SMART_SCAN_CANDIDATE_AI_CALLS,
        ge=1,
        le=SMART_SCAN_MAX_CANDIDATE_AI_CALLS,
    )
    seo_enabled: bool = False
    seo_site_limit: int = Field(
        default=SMART_SCAN_DATAFORSEO_DEFAULT_TARGET_LIMIT,
        ge=1,
        le=SMART_SCAN_DATAFORSEO_MAX_TARGET_LIMIT,
    )
    max_company_failure_rate: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def require_selected_work(self) -> SmartScanStartRequest:
        terms = list(dict.fromkeys(" ".join(term.split()) for term in self.search_terms
                                   if term.strip()))
        if any(len(term) > 100 for term in terms):
            raise ValueError("Search terms must be at most 100 characters each")
        self.search_terms = terms
        if self.source_ai_enabled is None:
            self.source_ai_enabled = self.ai_enabled and self.due_hydration_enabled
        if self.source_ai_enabled and not self.due_hydration_enabled:
            raise ValueError("Source AI requires Due Hydration")
        if self.microlink_enabled is None:
            self.microlink_enabled = self.due_hydration_enabled
        if self.microlink_enabled and not self.due_hydration_enabled:
            raise ValueError("Microlink fallback requires Due Hydration")
        if not (self.due_hydration_enabled or self.people_hydration_enabled
                or self.ai_enabled or self.seo_enabled or self.paid_ads_enabled
                or self.microlink_hydration.enabled):
            raise ValueError("Select Due Hydration, Paid ads, People Hydration, Use AI or Use SEO")
        return self


class WebScanEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_hosts_per_source: int = Field(default=10_000, ge=1, le=10_000)
    max_network_inspections: int = Field(default=1_000, ge=1, le=10_000)
    free_intelligence: bool = True
    agency_discovery: bool = True
    people_hydration_enabled: bool = False
    microlink_enabled: bool = False


class SourceScoutRequest(BaseModel):
    max_suggestions: int = Field(default=DEFAULT_SCOUT_SUGGESTIONS, ge=1, le=12)
    max_tool_calls: int = Field(default=DEFAULT_SCOUT_TOOL_CALLS, ge=1, le=10)
    keyword_combinations: int = Field(
        default=DEFAULT_SCOUT_KEYWORD_COMBINATIONS,
        ge=100,
        le=500,
    )


class DiscoveryMethodRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal[
        "company_intelligence",
        "tenant_footprints",
        "directories",
        "portfolios",
        "customer_lists",
        "searches",
        "public_graph",
    ]
    suggestion_ids: list[int] = Field(default_factory=list, max_length=25)
    scanner_ids: list[str] = Field(default_factory=list, max_length=len(FRONTIER_SCANNER_IDS))
    feed_source_ids: list[str] = Field(default_factory=list, max_length=25)
    lookback_days: int = Field(default=7, ge=1, le=90)
    max_feed_items: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=50, ge=1, le=50)
    max_candidates: int = Field(default=500, ge=1, le=500)
    max_network_inspections: int = Field(default=500, ge=1, le=500)
    ai_resolution: bool = True
    hydrate_companies: bool = True
    people_hydration_enabled: bool = True  # Legacy requests; UI explicitly opts in/out.
    microlink_enabled: bool = True  # Legacy requests; UI explicitly opts in/out.
    max_ai_tool_calls: int = Field(default=10, ge=1, le=10)
    max_ai_reviews: int = Field(default=50, ge=0, le=50)
    max_ai_web_reviews: int = Field(default=20, ge=0, le=20)

    @field_validator("suggestion_ids")
    @classmethod
    def unique_suggestion_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("Suggestion ids must be positive")
        return list(dict.fromkeys(value))

    @field_validator("scanner_ids", "feed_source_ids")
    @classmethod
    def unique_bounded_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if any(len(item) > 120 for item in normalized):
            raise ValueError("Scanner and feed source ids cannot exceed 120 characters")
        return normalized

    @model_validator(mode="after")
    def validate_method_scope(self) -> DiscoveryMethodRunRequest:
        if self.method in SERVER_OWNED_DISCOVERY_METHODS:
            if self.suggestion_ids:
                raise ValueError(f"{self.method} runs do not accept suggestion ids")
        elif not self.suggestion_ids:
            raise ValueError("Select at least one accepted source")
        if self.method == "company_intelligence":
            if not self.scanner_ids:
                raise ValueError("Select at least one company intelligence scanner")
            unknown = sorted(set(self.scanner_ids) - set(FRONTIER_SCANNER_IDS))
            if unknown:
                raise ValueError(f"Unknown company scanners: {', '.join(unknown)}")
        elif self.scanner_ids:
            raise ValueError("Company scanner ids are only valid for company intelligence")
        if self.method == "searches" and not self.ai_resolution:
            raise ValueError("Repeatable searches require cited AI resolution")
        if self.max_ai_web_reviews > self.max_ai_reviews:
            raise ValueError("AI web reviews cannot exceed total AI reviews")
        return self


class SecretKeyUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=500)


class DataForSEOCredentialsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=500)


class PeopleHydrationEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_limit: int = Field(default=TREG_PEOPLE_MAX_COHORT_SIZE, ge=1, le=2_500)
    people_limit: int = Field(
        default=DEFAULT_PEOPLE_LIMIT,
        ge=2,
        le=TREG_PEOPLE_MAX_PEOPLE_LIMIT,
    )
    email_limit: Literal[0] = 0
    email_max_cost_usd: Decimal = Field(
        default=DEFAULT_EMAIL_MAX_COST_USD,
        ge=Decimal("0.001"),
        le=Decimal("0.08"),
    )


class PeopleHydrationRunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str = Field(min_length=8, max_length=100)
    confirmed: bool
    confirmed_max_usd: Decimal = Field(ge=Decimal("0"), le=Decimal("500"))


class AdDiscoveryRunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str = Field(min_length=8, max_length=100)
    confirmed: bool
    confirmed_max_usd: Decimal = Field(ge=Decimal("0.10"), le=Decimal("100.00"))


class AdDomainLinkReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["verified", "probable", "ambiguous", "rejected"]
    review_note: str = Field(default="", max_length=2_000)
    reviewer: str = Field(default="operator", max_length=200)


class ScanRequest(BaseModel):
    sources: list[str] | None = None
    only_hosts: list[str] | None = Field(default=None, max_length=10_000)
    max_hosts_per_source: int = Field(default=100, ge=1, le=10_000)
    max_network_inspections: int = Field(default=0, ge=0, le=10_000)
    reuse_core_only: bool = False
    adaptive_refresh: bool = False
    manual_company_refresh: bool = False
    manual_tag_refresh: bool = False
    refresh_tag: str = Field(default="", max_length=40)
    refresh_mission_id: str = "beings"
    refresh_candidate_ids: list[int] = Field(default_factory=list, max_length=10_000)
    people_hydration_enabled: bool = True  # Preserve legacy direct callers.
    microlink_enabled: bool = True
    concurrency: int = Field(default=20, ge=1, le=60)
    pages_per_site: int = Field(default=DEFAULT_SITE_RESEARCH_PAGES, ge=1, le=8)
    english_only: bool = True
    free_intelligence: bool = True
    max_sitemaps_per_site: int = Field(default=4, ge=1, le=12)
    max_common_crawl_lookups: int = Field(default=30, ge=0, le=200)
    max_news_lookups: int = Field(default=5, ge=0, le=50)
    max_frontier_scans: int = Field(default=20, ge=0, le=500)
    frontier_scanner_ids: list[str] = Field(
        default_factory=list, max_length=len(FRONTIER_SCANNER_IDS)
    )
    respect_frontier_schedule: bool = False
    frontier_scanner_manifest: dict[str, list[str]] = Field(default_factory=dict, max_length=25)
    agency_discovery: bool = False
    ai: bool = False
    ai_web: bool = False
    min_ai_score: int = Field(default=45, ge=0, le=100)
    max_ai_calls: int = Field(default=40, ge=0, le=500)
    max_ai_web_calls: int = Field(default=8, ge=0, le=100)
    evergreen_discovery: bool = False
    evergreen_max_suggestions: int = Field(default=12, ge=1, le=12)
    evergreen_max_tool_calls: int = Field(default=10, ge=1, le=10)

    @model_validator(mode="before")
    @classmethod
    def restore_saved_scanner_ids(cls, value):
        if isinstance(value, Mapping) and value.get("frontier_scanner_manifest", {}) is None:
            value = {**value, "frontier_scanner_manifest": {}}
        if (isinstance(value, Mapping) and "frontier_scanner_ids" not in value
                and "frontier_scanners" in value):
            return {**value, "frontier_scanner_ids": value.get("frontier_scanners") or []}
        return value

    @model_validator(mode="after")
    def validate_manifest(self):
        self.frontier_scanner_manifest = validate_manifest(
            self.frontier_scanner_manifest, self.only_hosts,
            reuse_core_only=self.reuse_core_only, respect_schedule=self.respect_frontier_schedule,
            maximum=self.max_frontier_scans,
        )
        if self.frontier_scanner_manifest:
            self.frontier_scanner_ids = list(dict.fromkeys(
                scanner for ids in self.frontier_scanner_manifest.values() for scanner in ids
            ))
        return self

    @field_validator("frontier_scanner_ids")
    @classmethod
    def validate_frontier_scanner_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        unknown = sorted(set(normalized) - set(FRONTIER_SCANNER_IDS))
        if unknown:
            raise ValueError(f"Unknown frontier scanners: {', '.join(unknown)}")
        return normalized


class ImportScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)
    tag: str = Field(default="", max_length=40)


import_scan_lock = asyncio.Lock()


class ScanManager:
    def __init__(self) -> None:
        self.job_id: str | None = None
        self.task: asyncio.Task[None] | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._process_group_id: int | None = None
        self.retry_count = 0
        self.started_at: datetime | None = None
        self.shutting_down = False
        self.actions: deque[dict[str, object]] = deque(maxlen=5_000)
        self.action_sequence = 0
        self.state: dict = {"status": "idle", "message": "Ready"}
        self.last_diagnostic_step: dict = {}

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(
        self,
        request: ScanRequest,
        sources: list[str],
        max_candidates: int,
        *,
        reset_retries: bool = True,
        force_refresh: bool = False,
        include_people_hydration: bool = False,
    ) -> dict:
        if self.running:
            raise HTTPException(status_code=409, detail="A scan is already running")
        if request.ai_web and not request.ai:
            raise HTTPException(status_code=422, detail="AI web research requires AI review")
        if request.ai and not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=422, detail="OPENAI_API_KEY is not configured")
        if request.evergreen_discovery and not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(
                status_code=422,
                detail="Evergreen source discovery requires OPENAI_API_KEY",
            )
        if request.evergreen_discovery and source_scout_manager.running:
            raise HTTPException(status_code=409, detail="A source scout is already running")
        if not sources and not request.agency_discovery and not request.only_hosts:
            raise HTTPException(
                status_code=422,
                detail="Select an enabled source, an explicit persisted host, or public graph",
            )
        if reset_retries:
            self.retry_count = 0
            self.actions.clear()
            self.action_sequence = 0
        self.shutting_down = False
        self.started_at = datetime.now(UTC)
        self.last_diagnostic_step = {}
        logger.info(
            "starting scan sources=%d max_candidates=%d ai=%s free_intelligence=%s",
            len(sources),
            max_candidates,
            request.ai,
            request.free_intelligence,
        )
        self.state = {
            "status": "running",
            "message": "Scan running",
            "sources": sources,
            "source_count": len(sources),
            "processed": 0,
            "max_candidates": max_candidates,
            "progress_percent": 0.0,
        }
        self.task = asyncio.create_task(
            self._run(
                request,
                sources,
                force_refresh=force_refresh,
                include_people_hydration=include_people_hydration,
            )
        )
        if self.watchdog_task is None or self.watchdog_task.done():
            self.watchdog_task = asyncio.create_task(self._watchdog())
        return self.state

    def _record_action(self, payload: Mapping[str, object]) -> None:
        self.action_sequence += 1
        self.actions.append(
            {
                "id": self.action_sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "message": str(payload.get("message") or "Action")[:240],
                "detail": str(payload.get("detail") or "")[:500],
                "level": str(payload.get("level") or "item")[:24],
            }
        )

    async def _read_process_stream(
        self,
        stream: asyncio.StreamReader | None,
        *,
        parse_actions: bool,
    ) -> str:
        if stream is None:
            return ""
        output: deque[str] = deque(maxlen=2_000)
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if parse_actions and SCAN_ID.get() and text.startswith(MARKER):
                try:
                    record = json.loads(text.removeprefix(MARKER))
                except ValueError:
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("event"), str):
                    continue
                event = record.pop("event")
                if event == "company_step":
                    self.last_diagnostic_step = {
                        "step": record.get("step"), "status": record.get("status"),
                        "seen": monotonic(),
                    }
                emit_scan_diagnostic(
                    SCAN_ID.get(), event, job_id=self.job_id,
                    attempt=self.retry_count + 1, **record,
                )
                continue
            action = decode_scan_action(text) if parse_actions else None
            if action is not None:
                self._record_action(action)
                continue
            output.append(text)
        return "\n".join(output)

    async def _watchdog(self) -> None:
        def read_progress():
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                return store.active_run_progress(self.job_id)

        while self.task is not None and not self.task.done():
            await asyncio.sleep(10)
            if self.process is None or self.process.returncode is not None:
                continue
            started_at = self.started_at or datetime.now(UTC)
            if (datetime.now(UTC) - started_at).total_seconds() < SCAN_STARTUP_GRACE_SECONDS:
                continue
            scan_id = SCAN_ID.get()
            read_started = monotonic()
            try:
                progress = await asyncio.to_thread(read_progress)
            except sqlite3.Error as exc:
                emit_scan_diagnostic(
                    scan_id, "watchdog_progress_read", job_id=self.job_id,
                    attempt=self.retry_count + 1, seconds=round(monotonic() - read_started, 6),
                    **scan_diagnostic_error(exc),
                )
                raise
            read_elapsed = monotonic() - read_started
            if read_elapsed >= 1:
                emit_scan_diagnostic(
                    scan_id, "watchdog_progress_read", job_id=self.job_id,
                    attempt=self.retry_count + 1, seconds=round(read_elapsed, 6),
                )
            updated_at = progress.get("progress_updated_at")
            if not updated_at:
                continue
            age = (datetime.now(UTC) - datetime.fromisoformat(updated_at)).total_seconds()
            if age > SCAN_STALL_SECONDS:
                last_step = dict(self.last_diagnostic_step)
                if last_step:
                    last_step["seconds_ago"] = round(monotonic() - last_step.pop("seen"), 3)
                emit_scan_diagnostic(
                    scan_id, "watchdog_termination", job_id=self.job_id,
                    attempt=self.retry_count + 1, run_id=progress.get("run_id"),
                    phase=progress.get("phase"), progress_age_seconds=round(age, 3),
                    last_child_step=last_step,
                )
                logger.error(
                    "scan stalled run_id=%s phase=%s progress_age_seconds=%.0f",
                    progress.get("run_id"),
                    progress.get("phase"),
                    age,
                )
                await self._terminate_process()

    async def recover_stale_runs(self) -> None:
        while True:
            try:
                if not self.running:
                    with LeadStore(database_path()) as store:
                        progress = store.active_run_progress(self.job_id)
                    updated_at = progress.get("progress_updated_at")
                    if progress and updated_at:
                        age = (
                            datetime.now(UTC) - datetime.fromisoformat(updated_at)
                        ).total_seconds()
                        if age > SCAN_STALL_SECONDS:
                            config = dict(progress.get("config") or {})
                            sources = list(config.get("apexes", []))
                            force_refresh = bool(config.get("force_refresh", False))
                            request = ScanRequest.model_validate(config)
                            with LeadStore(database_path()) as store:
                                run_id = int(progress["run_id"])
                                max_candidates = store.scan_capacity(
                                    sources,
                                    request.max_hosts_per_source,
                                )
                                store.release_company_scan_claims(
                                    run_id,
                                    "stale_worker_recovered",
                                )
                                store.finish_run(
                                    run_id,
                                    "failed",
                                    {"reason": "stale_worker_recovered", "retrying": True},
                                )
                            logger.warning(
                                "recovering stale scan run_id=%s age_seconds=%.0f",
                                progress["run_id"],
                                age,
                            )
                            self.retry_count = 0
                            await self.start(
                                request,
                                sources,
                                max_candidates,
                                force_refresh=force_refresh,
                                include_people_hydration=(
                                    request.people_hydration_enabled
                                    and not smart_scan_manager.active
                                ),
                            )
            except Exception:
                logger.exception("scan recovery loop failed")
            await asyncio.sleep(10)

    async def _retry_or_fail(
        self,
        request: ScanRequest,
        sources: list[str],
        max_candidates: int,
        reason: str,
        *,
        force_refresh: bool = False,
        include_people_hydration: bool = False,
    ) -> None:
        run_id = 0
        with LeadStore(database_path()) as store:
            progress = store.active_run_progress(self.job_id)
            if progress:
                run_id = int(progress["run_id"])
                store.release_run_scanner_claims(run_id, reason)
                store.release_company_scan_claims(run_id, reason)
                store.finish_run(
                    run_id,
                    "failed",
                    {"reason": reason, "retrying": self.retry_count < MAX_SCAN_RETRIES},
                )
        if self.retry_count >= MAX_SCAN_RETRIES:
            self.state = {
                "status": "failed",
                "message": f"Scan failed after {MAX_SCAN_RETRIES} retries",
                "error": " ".join(str(reason).split())[:2_000],
                "run_id": run_id or None,
            }
            logger.error("scan retry limit reached reason=%s", reason)
            return
        self.retry_count += 1
        logger.warning(
            "retrying scan attempt=%d/%d backoff_seconds=%d reason=%s",
            self.retry_count,
            MAX_SCAN_RETRIES,
            SCAN_RETRY_BACKOFF_SECONDS,
            reason,
        )
        self.state = {
            "status": "retrying",
            "message": f"Retrying scan ({self.retry_count}/{MAX_SCAN_RETRIES})",
            "retry_count": self.retry_count,
        }
        await asyncio.sleep(SCAN_RETRY_BACKOFF_SECONDS)
        self.task = None
        await self.start(
            request,
            sources,
            max_candidates,
            reset_retries=False,
            force_refresh=force_refresh,
            include_people_hydration=include_people_hydration,
        )
        retry_task = self.task
        if retry_task is not None and retry_task is not asyncio.current_task():
            await retry_task

    async def _run(
        self,
        request: ScanRequest,
        sources: list[str],
        *,
        force_refresh: bool = False,
        include_people_hydration: bool = False,
    ) -> None:
        started_state = dict(self.state)
        report_path = ROOT / "reports" / f"scan-{uuid.uuid4().hex[:12]}.md"
        json_report_path = ROOT / "reports" / f"scan-{uuid.uuid4().hex[:12]}.json"
        command = [
            sys.executable,
            "-m",
            "beings_leads",
            "scan",
            *sources,
            "--db",
            str(database_path()),
            "--report",
            str(report_path),
            "--json-report",
            str(json_report_path),
            "--database-ready",
            "--max-hosts-per-source",
            str(request.max_hosts_per_source),
            "--max-network-inspections",
            str(request.max_network_inspections),
            "--concurrency",
            str(request.concurrency),
            "--pages-per-site",
            str(request.pages_per_site),
            "--max-sitemaps-per-site",
            str(request.max_sitemaps_per_site),
            "--max-common-crawl-lookups",
            str(request.max_common_crawl_lookups),
            "--max-news-lookups",
            str(request.max_news_lookups),
            "--max-frontier-scans",
            str(request.max_frontier_scans),
            "--min-ai-score",
            str(request.min_ai_score),
            "--max-ai-calls",
            str(request.max_ai_calls),
            "--max-ai-web-calls",
            str(request.max_ai_web_calls),
        ]
        if not request.free_intelligence:
            command.append("--no-free-intelligence")
        if not include_people_hydration:
            command.append("--no-people-hydration")
        if not request.microlink_enabled:
            command.append("--no-microlink")
        if not request.english_only:
            command.append("--include-non-english")
        if request.adaptive_refresh:
            command.append("--adaptive-refresh")
        if request.reuse_core_only:
            command.append("--reuse-core-only")
        if request.ai:
            command.append("--ai")
        if request.ai_web:
            command.append("--ai-web")
        if request.agency_discovery:
            command.append("--agency-discovery")
        for scanner_id in request.frontier_scanner_ids:
            command.extend(["--frontier-scanner", scanner_id])
        if request.respect_frontier_schedule:
            command.append("--respect-frontier-schedule")
        if request.frontier_scanner_manifest:
            command.extend(["--frontier-scanner-manifest-json",
                            json.dumps(request.frontier_scanner_manifest)])
        if force_refresh:
            command.append("--force-refresh")
        if request.manual_company_refresh:
            command.append("--manual-company-refresh")
        if request.manual_tag_refresh:
            command.append("--manual-tag-refresh")
        host_input = None
        if len(request.only_hosts or []) > 100:
            command.append("--only-hosts-stdin")
            host_input = json.dumps(request.only_hosts).encode("utf-8")
        else:
            for host in request.only_hosts or []:
                command.extend(["--only-host", host])
        if self.job_id:
            command.extend(["--job-id", self.job_id])
        stdout_task: asyncio.Task[str] | None = None
        stderr_task: asyncio.Task[str] | None = None
        try:
            scan_id = SCAN_ID.get()
            emit_scan_diagnostic(
                scan_id, "company_attempt_started", job_id=self.job_id,
                attempt=self.retry_count + 1, offered_hosts=len(request.only_hosts or []),
                concurrency=request.concurrency,
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", CHILD_ENV: "1" if scan_id else "0"},
                start_new_session=os.name == "posix",
                **({"stdin": asyncio.subprocess.PIPE} if host_input is not None else {}),
                limit=2_000_000,
            )
            self.process = process
            # Only groups created by this manager may be signalled as a group.
            pid = getattr(process, "pid", None)
            self._process_group_id = (
                pid if os.name == "posix" and isinstance(pid, int) and pid > 1 else None
            )
            if hasattr(process, "wait") and hasattr(process, "stdout"):
                stdout_task = asyncio.create_task(
                    self._read_process_stream(process.stdout, parse_actions=False)
                )
                stderr_task = asyncio.create_task(
                    self._read_process_stream(process.stderr, parse_actions=True)
                )
                if host_input is not None:
                    process.stdin.write(host_input)
                    await process.stdin.drain()
                    process.stdin.close()
                await self._wait_for_process_exit(process)
                # Keep pipe readers alive on pause, including after the CLI exits.
                # The cancellation handler terminates descendants and drains EOF.
                stdout = await asyncio.shield(stdout_task)
                stderr = await asyncio.shield(stderr_task)
            else:  # Lightweight test doubles and compatibility adapters.
                stdout_bytes, stderr_bytes = await process.communicate()
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            message = stderr[-2_000:].strip()
            emit_scan_diagnostic(
                scan_id, "company_attempt_exited", job_id=self.job_id,
                attempt=self.retry_count + 1, returncode=process.returncode,
            )
            if process.returncode == 0:
                with LeadStore(database_path()) as store:
                    run = (
                        store.run_metadata_for_job(self.job_id)
                        if self.job_id
                        else store.run_metadata()
                    )
                processed = int(run.get("summary", {}).get("candidates_discovered", 0))
                evergreen: dict = {"status": "disabled", "suggestions": 0}
                if request.evergreen_discovery:
                    self.state = {
                        "status": "running",
                        "message": "Finding new source ecosystems",
                        "phase": "evergreen_discovery",
                        "processed": 0,
                        "total_candidates": request.evergreen_max_suggestions,
                        "progress_percent": 96.0,
                        "source_count": started_state.get("source_count", len(sources)),
                    }
                    evergreen = await source_scout_manager.run_inline(
                        SourceScoutRequest(
                            max_suggestions=request.evergreen_max_suggestions,
                            max_tool_calls=request.evergreen_max_tool_calls,
                            keyword_combinations=DEFAULT_SCOUT_KEYWORD_COMBINATIONS,
                        ),
                        trigger="scan",
                    )
                discovered = int(evergreen.get("suggestions", 0))
                discovery_failed = evergreen.get("status") == "failed"
                persisted_status = "partial" if run.get("status") == "partial" else "complete"
                if persisted_status == "partial":
                    completion_message = "Scan completed with partial results"
                else:
                    completion_message = "Scan completed"
                if discovery_failed:
                    completion_message += "; evergreen discovery failed"
                elif request.evergreen_discovery:
                    completion_message += f" · {discovered} new source ideas"
                people_hydration: dict[str, Any] = {
                    "status": "not_requested",
                    "message": "People hydration is owned by the parent workflow",
                }
                if include_people_hydration:
                    self.state = {
                        **self.state,
                        "status": "running",
                        "phase": "people_hydration",
                        "message": "Hydrating people for ranked companies touched by this scan",
                        "progress_percent": 97.0,
                    }
                    try:
                        people_hydration = await _run_people_hydration_for_manual_scan(
                            int(run.get("id") or 0)
                        )
                    except Exception as exc:
                        logger.error(
                            "manual people hydration failed type=%s",
                            type(exc).__name__,
                        )
                        people_hydration = {
                            "status": "failed",
                            "message": "People hydration could not complete",
                            "actual_cost_usd": 0.0,
                        }
                    if people_hydration.get("status") == "failed" or int(
                        people_hydration.get("failed_companies") or 0
                    ):
                        completion_message += "; people hydration had recoverable gaps"
                if request.manual_tag_refresh:
                    def publish_tag():
                        from .company_tags import rescore_tag
                        with LeadStore(database_path()) as store:
                            return rescore_tag(
                                store, request.refresh_tag, request.refresh_mission_id,
                                candidate_ids=request.refresh_candidate_ids,
                            )

                    try:
                        tag_scores = await asyncio.to_thread(publish_tag)
                    except Exception:
                        # Collection is already committed. A score-only error must not
                        # restart an explicit refresh and repeat website requests.
                        logger.exception("Tag scan completed but local scoring failed")
                        tag_scores = {"errors": ["local_scoring_failed"]}
                    if tag_scores["errors"]:
                        completion_message += "; some scores could not be refreshed"
                self.state = {
                    "status": persisted_status,
                    "message": completion_message,
                    "output": stdout.strip(),
                    "run_id": run.get("id"),
                    "processed": processed,
                    "max_candidates": started_state.get("max_candidates", processed),
                    "progress_percent": 100.0,
                    "source_count": started_state.get("source_count", len(sources)),
                    "evergreen": evergreen,
                    "people_hydration": people_hydration,
                }
                logger.info(
                    "scan completed run_id=%s processed=%d evergreen=%s",
                    run.get("id"),
                    processed,
                    request.evergreen_discovery,
                )
            else:
                logger.error(
                    "scan failed returncode=%s message=%s",
                    process.returncode,
                    message or "unknown",
                )
                await self._retry_or_fail(
                    request,
                    sources,
                    int(started_state.get("max_candidates", 0)),
                    message or f"worker exited with status {process.returncode}",
                    force_refresh=force_refresh,
                    include_people_hydration=include_people_hydration,
                )
        except asyncio.CancelledError:
            await self._terminate_process()
            stream_tasks = [
                task for task in (stdout_task, stderr_task) if task is not None
            ]
            if stream_tasks:
                await asyncio.gather(*stream_tasks, return_exceptions=True)
            if self.job_id:
                with LeadStore(database_path()) as store:
                    progress = store.active_run_progress(self.job_id)
                    if progress:
                        reason = (
                            "local_worker_shutdown"
                            if self.shutting_down
                            else "scan_cancelled"
                        )
                        store.release_run_scanner_claims(int(progress["run_id"]), reason)
                        store.finish_run_with_company_scan_work(
                            int(progress["run_id"]),
                            "partial",
                            {
                                "interrupted": True,
                                "reason": reason,
                                "phase": progress.get("phase"),
                                "processed": int(progress.get("processed") or 0),
                            },
                            completed_candidate_ids=[],
                            failed_candidate_ids=[],
                            failure_reason=reason,
                        )
            self.state = {
                **self.state,
                "status": "failed",
                "message": "Scan stopped; completed work is preserved",
            }
            raise
        except Exception as exc:
            logger.exception("scan crashed")
            await self._terminate_process()
            await asyncio.gather(
                *(task for task in (stdout_task, stderr_task) if task is not None),
                return_exceptions=True,
            )
            await self._retry_or_fail(
                request,
                sources,
                int(started_state.get("max_candidates", 0)),
                f"{type(exc).__name__}: {exc}",
                force_refresh=force_refresh,
                include_people_hydration=include_people_hydration,
            )
        finally:
            self.process = None
            self._process_group_id = None

    async def _wait_for_process_exit(self, process: asyncio.subprocess.Process) -> None:
        # Process.wait() may wait for inherited pipes even after the CLI has exited.
        # Reap that leader independently, then close only its leftover descendants.
        waiter = asyncio.create_task(process.wait())
        try:
            while not waiter.done() and process.returncode is None:
                await asyncio.wait({waiter}, timeout=0.1)
            if self._process_group_id is not None and process.returncode is not None:
                await self._terminate_process()
            await waiter
        finally:
            if not waiter.done():
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter

    async def _terminate_process(self) -> None:
        process = self.process
        if process is None:
            return
        group_id = self._process_group_id
        if group_id is None and process.returncode is not None:
            return
        try:
            if group_id is not None:
                os.killpg(group_id, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            logger.warning("scan worker did not terminate; killing job_id=%s", self.job_id)
        finally:
            # The CLI may already have exited while pure descendants retain its pipes.
            # Finish only this job's dedicated group before waiting for stream EOF.
            with suppress(ProcessLookupError):
                if group_id is not None:
                    os.killpg(group_id, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
        if process.returncode is None:
            await process.wait()

    async def shutdown(self) -> None:
        self.shutting_down = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        else:
            await self._terminate_process()
        if self.watchdog_task is not None and not self.watchdog_task.done():
            self.watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.watchdog_task
        if self.job_id:
            with LeadStore(database_path()) as store:
                progress = store.active_run_progress(self.job_id)
                if progress:
                    run_id = int(progress["run_id"])
                    store.release_company_scan_claims(run_id, "local_worker_shutdown")
                    store.finish_run(
                        run_id,
                        "failed",
                        {"reason": "local_worker_shutdown"},
                    )


class ScanPool:
    """Run independent scan jobs concurrently while preserving legacy status access."""

    def __init__(self) -> None:
        self.jobs: dict[str, ScanManager] = {}
        self.finished_states: dict[str, dict] = {}
        self.last_state: dict = {"status": "idle", "message": "Ready"}
        self.compat_task: object | None = None
        self.compat_state: dict | None = None

    @property
    def running(self) -> bool:
        if self.compat_task is not None:
            return not self.compat_task.done()
        return any(job.running for job in self.jobs.values())

    @property
    def task(self) -> object | None:
        if self.compat_task is not None:
            return self.compat_task
        return next((job.task for job in self.jobs.values() if job.running), None)

    @task.setter
    def task(self, value: object | None) -> None:
        self.compat_task = value

    @property
    def state(self) -> dict:
        if self.compat_state is not None:
            return self.compat_state
        active = [job.state for job in self.jobs.values() if job.running]
        if active:
            state = dict(active[0])
            state["active_count"] = len(active)
            return state
        return self.last_state

    @state.setter
    def state(self, value: dict) -> None:
        self.compat_state = value
        self.last_state = value

    async def start(
        self,
        request: ScanRequest,
        sources: list[str],
        max_candidates: int,
        *,
        reset_retries: bool = True,
        force_refresh: bool = False,
        include_people_hydration: bool = False,
    ) -> dict:
        job_id = uuid.uuid4().hex[:12]
        job = ScanManager()
        job.job_id = job_id
        self.jobs[job_id] = job
        result = await job.start(
            request,
            sources,
            max_candidates,
            reset_retries=reset_retries,
            force_refresh=force_refresh,
            include_people_hydration=include_people_hydration,
        )
        job.task.add_done_callback(lambda _: self._job_finished(job_id, job))
        result["job_id"] = job_id
        result["active_count"] = sum(item.running for item in self.jobs.values())
        self.last_state = result
        return result

    def _job_finished(self, job_id: str, job: ScanManager) -> None:
        if job.running:
            return
        self.last_state = dict(job.state)
        self.finished_states[job_id] = dict(job.state)
        while len(self.finished_states) > 100:
            self.finished_states.pop(next(iter(self.finished_states)))
        self.jobs.pop(job_id, None)

    def status_for(self, job_id: str | None = None) -> dict:
        if job_id:
            job = self.jobs.get(job_id)
            if job:
                return {**dict(job.state), "job_id": job_id}
            if job_id in self.finished_states:
                return {**dict(self.finished_states[job_id]), "job_id": job_id}
        return dict(self.state)

    async def recover_stale_runs(self) -> None:
        while True:
            try:
                if not self.running:
                    with LeadStore(database_path()) as store:
                        progress = store.active_run_progress()
                    updated_at = progress.get("progress_updated_at")
                    if progress and updated_at:
                        age = (
                            datetime.now(UTC) - datetime.fromisoformat(updated_at)
                        ).total_seconds()
                        if age > SCAN_STALL_SECONDS:
                            config = dict(progress.get("config") or {})
                            sources = list(config.get("apexes", []))
                            force_refresh = bool(config.get("force_refresh", False))
                            request = ScanRequest.model_validate(config)
                            with LeadStore(database_path()) as store:
                                run_id = int(progress["run_id"])
                                max_candidates = store.scan_capacity(
                                    sources,
                                    request.max_hosts_per_source,
                                )
                                store.release_company_scan_claims(
                                    run_id,
                                    "stale_worker_recovered",
                                )
                                store.finish_run(
                                    run_id,
                                    "failed",
                                    {"reason": "stale_worker_recovered", "retrying": True},
                                )
                            logger.warning(
                                "recovering stale scan run_id=%s age_seconds=%.0f",
                                progress["run_id"],
                                age,
                            )
                            await self.start(
                                request,
                                sources,
                                max_candidates,
                                force_refresh=force_refresh,
                                include_people_hydration=(
                                    request.people_hydration_enabled
                                    and not smart_scan_manager.active
                                ),
                            )
            except Exception:
                logger.exception("scan recovery loop failed")
            await asyncio.sleep(10)

    async def shutdown(self) -> None:
        jobs = list(self.jobs.values())
        if jobs:
            await asyncio.gather(
                *(job.shutdown() for job in jobs),
                return_exceptions=True,
            )


scan_manager = ScanPool()


def _source_scout_diagnostics(value: object) -> dict[str, Any]:
    """Extract the scout's bounded, JSON-safe family diagnostics when available."""

    diagnostics = getattr(value, "diagnostics", {})
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


class SourceScoutManager:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.scheduler_task: asyncio.Task[None] | None = None
        self.inline_running = False
        self.state: dict = {"status": "idle", "message": "Ready"}

    @property
    def running(self) -> bool:
        return self.inline_running or (self.task is not None and not self.task.done())

    def _model(self) -> str:
        return os.getenv(
            "BEINGS_LEADS_SOURCE_MODEL",
            os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
        )

    async def start(self, request: SourceScoutRequest) -> dict:
        if self.running:
            raise HTTPException(status_code=409, detail="A source scout is already running")
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=422, detail="OPENAI_API_KEY is not configured")
        model = self._model()
        logger.info(
            "starting source scout model=%s suggestions=%d tool_calls=%d keyword_combinations=%d",
            model,
            request.max_suggestions,
            request.max_tool_calls,
            request.keyword_combinations,
        )
        self.state = {
            "status": "running",
            "message": "Scouting sources",
            "model": model,
            "started_at": datetime.now(UTC).isoformat(),
            "max_suggestions": request.max_suggestions,
            "max_tool_calls": request.max_tool_calls,
            "keyword_combinations": request.keyword_combinations,
        }
        self.task = asyncio.create_task(self._run(request, model))
        return self.state

    async def _run(self, request: SourceScoutRequest, model: str) -> None:
        await self._execute(request, model, trigger="manual")

    async def run_inline(
        self,
        request: SourceScoutRequest,
        *,
        trigger: str,
    ) -> dict:
        if self.running:
            return {
                "status": "failed",
                "message": "Another source scout is already running",
                "suggestions": 0,
                "keyword_combinations": request.keyword_combinations,
            }
        if not os.getenv("OPENAI_API_KEY"):
            return {
                "status": "failed",
                "message": "OPENAI_API_KEY is not configured",
                "suggestions": 0,
                "keyword_combinations": request.keyword_combinations,
            }
        self.inline_running = True
        model = self._model()
        self.state = {
            "status": "running",
            "message": "Scouting new source ecosystems",
            "model": model,
            "trigger": trigger,
            "started_at": datetime.now(UTC).isoformat(),
            "max_suggestions": request.max_suggestions,
            "max_tool_calls": request.max_tool_calls,
            "keyword_combinations": request.keyword_combinations,
        }
        try:
            return await self._execute(request, model, trigger=trigger)
        finally:
            self.inline_running = False

    async def _execute(
        self,
        request: SourceScoutRequest,
        model: str,
        *,
        trigger: str,
    ) -> dict:
        with LeadStore(database_path()) as store:
            run_id = store.start_scout_run(model, request.max_tool_calls)
        try:
            with LeadStore(database_path()) as store:
                existing_sources = [source["apex"] for source in store.list_sources()]
                suggestion_rows = store.list_source_suggestions()
                existing_suggestions = list(
                    dict.fromkeys(
                        str(value)
                        for suggestion in suggestion_rows
                        for value in (
                            suggestion.get("source_key"),
                            suggestion.get("source_identity_key"),
                        )
                        if value
                    )
                )
                prior_source_outcomes = store.source_scout_source_context(limit=200)
                recent_query_fingerprints = store.recent_source_scout_query_fingerprints()
            suggestions = await scout_sources(
                existing_sources,
                existing_suggestions,
                model=model,
                max_suggestions=request.max_suggestions,
                max_tool_calls=request.max_tool_calls,
                keyword_combinations=request.keyword_combinations,
                cycle=run_id,
                prior_source_outcomes=prior_source_outcomes,
                recent_query_fingerprints=recent_query_fingerprints,
            )
            coverage = _source_scout_output_coverage(suggestions)
            diagnostics = _source_scout_diagnostics(suggestions)
            failed_families = diagnostics.get("failed_source_families") or {}
            budget_overruns = diagnostics.get("tool_budget_overrun_source_families") or []
            run_status = "partial" if failed_families or budget_overruns else "complete"
            run_summary = {
                "suggestions": len(suggestions),
                "max_tool_calls": request.max_tool_calls,
                "keyword_combinations": request.keyword_combinations,
                "trigger": trigger,
                **diagnostics,
                **coverage,
            }
            with LeadStore(database_path()) as store:
                store.upsert_source_suggestions(run_id, suggestions)
                store.finish_scout_run(
                    run_id,
                    run_status,
                    run_summary,
                )
            attempted_count = len(diagnostics.get("attempted_source_families") or [])
            searched_count = len(diagnostics.get("searched_source_families") or [])
            failed_count = len(failed_families)
            budget_overrun_count = len(budget_overruns)
            self.state = {
                "status": run_status,
                "message": (
                    (
                        f"Source scout partially complete; {failed_count} of "
                        f"{attempted_count or failed_count} buyer-source families failed"
                        if failed_count
                        else (
                            "Source scout complete with provider budget telemetry warnings in "
                            f"{budget_overrun_count} buyer-source families"
                        )
                    )
                    if run_status == "partial"
                    else (
                        "Source scout complete"
                        if suggestions
                        else (
                            (
                                "Source scout complete; no novel sources passed the evidence "
                                f"filters after {searched_count} verified family searches"
                            )
                            if searched_count
                            else (
                                "Source scout complete; no novel sources passed the evidence "
                                "filters"
                            )
                        )
                    )
                ),
                "run_id": run_id,
                "suggestions": len(suggestions),
                "max_tool_calls": request.max_tool_calls,
                "keyword_combinations": request.keyword_combinations,
                "trigger": trigger,
                **diagnostics,
                **coverage,
            }
        except Exception as exc:
            diagnostics = _source_scout_diagnostics(exc)
            with LeadStore(database_path()) as store:
                store.finish_scout_run(
                    run_id,
                    "failed",
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "keyword_combinations": request.keyword_combinations,
                        **diagnostics,
                    },
                )
            self.state = {
                "status": "failed",
                "message": f"{type(exc).__name__}: {exc}",
                "run_id": run_id,
                "suggestions": 0,
                "max_tool_calls": request.max_tool_calls,
                "keyword_combinations": request.keyword_combinations,
                "trigger": trigger,
                **diagnostics,
            }
            logger.exception("source scout failed run_id=%s trigger=%s", run_id, trigger)
        return self.state

    def start_scheduler(self) -> None:
        hours = int(os.getenv("BEINGS_LEADS_AUTO_SCOUT_HOURS", "0") or 0)
        if hours > 0 and os.getenv("OPENAI_API_KEY") and self.scheduler_task is None:
            self.scheduler_task = asyncio.create_task(self._scheduler(hours))

    async def _scheduler(self, hours: int) -> None:
        while True:
            if not self.running and not bool(
                getattr(
                    smart_scan_manager,
                    "active",
                    getattr(smart_scan_manager, "running", False),
                )
            ):
                with LeadStore(database_path()) as store:
                    latest = store.intelligence_payload().get("latest_scout")
                last_value = (latest or {}).get("finished_at") or (latest or {}).get("started_at")
                last = datetime.fromisoformat(last_value) if last_value else None
                due = last is None or datetime.now(UTC) - last >= timedelta(hours=hours)
                if due:
                    await self.start(
                        SourceScoutRequest(
                            keyword_combinations=DEFAULT_SCOUT_KEYWORD_COMBINATIONS,
                        )
                    )
            await asyncio.sleep(min(max(hours * 300, 60), 3_600))

    async def stop_scheduler(self) -> None:
        if self.scheduler_task is not None:
            self.scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.scheduler_task
            self.scheduler_task = None


source_scout_manager = SourceScoutManager()


def _source_suggestion(row: dict[str, Any]) -> SourceSuggestion:
    return SourceSuggestion.model_validate(row)


def _method_tab(method_id: str) -> dict[str, object] | None:
    return next((item for item in DISCOVERY_METHOD_TABS if item["id"] == method_id), None)


def _method_for_suggestion(suggestion: SourceSuggestion) -> str | None:
    return next(
        (
            str(item["id"])
            for item in DISCOVERY_METHOD_TABS
            if suggestion.source_kind in set(item.get("source_kinds") or [])
            and item["id"] in EXECUTABLE_DISCOVERY_METHODS
        ),
        None,
    )


def _method_accepts_suggestion(method_id: str, suggestion: SourceSuggestion) -> bool:
    tab = _method_tab(method_id) or {}
    return suggestion.source_kind in set(tab.get("source_kinds") or [])


def _frontier_scanner_payloads() -> list[dict[str, object]]:
    """Expose the executable registry verbatim so backend-only scanners cannot stay hidden."""

    registered_ids = tuple(definition.scanner_id for definition in SCANNER_DEFINITIONS)
    if registered_ids != tuple(FRONTIER_SCANNER_IDS):
        raise RuntimeError(
            "Discovery company-scanner catalogue is out of sync with the executable registry"
        )
    return [
        {
            "id": definition.scanner_id,
            "version": definition.version,
            "cost_class": definition.cost_class,
            "ttl_hours": definition.ttl_seconds // 3_600,
            "max_requests": definition.max_requests,
            "max_response_bytes": definition.max_response_bytes,
            "applicability_inputs": list(definition.applicability_inputs),
            "dimensions": list(definition.eig_dimensions),
            "safe_claim": definition.safe_claim,
            "prohibited_claims": list(definition.prohibited_claims),
        }
        for definition in SCANNER_DEFINITIONS
    ]


def _discovery_source_total(
    request: DiscoveryMethodRunRequest,
    suggestions: list[SourceSuggestion],
) -> int:
    if request.method == "company_intelligence":
        return len(request.scanner_ids)
    if request.method == "public_graph":
        return 2
    return len(suggestions)


def _market_endpoint_urls(
    source_id: str,
    *,
    lookback_days: int,
    max_items: int,
) -> tuple[str, ...] | None:
    if source_id != "gdelt_doc_metadata_v2":
        return None
    policy = require_market_source_policy(source_id)
    query = (
        '("artificial intelligence" OR "generative AI" OR "AI startup") '
        "(funding OR launch OR partnership OR expansion OR campaign OR acquisition)"
    )
    return (
        str(
            httpx.URL(
                policy.endpoint_urls[0],
                params={
                    "query": query,
                    "mode": "ArtList",
                    "format": "json",
                    "sort": "DateDesc",
                    "timespan": f"{lookback_days}d",
                    "maxrecords": str(max_items),
                },
            )
        ),
    )


async def _poll_market_feed(
    client: httpx.AsyncClient,
    source_id: str,
    *,
    lookback_days: int,
    max_items: int,
) -> dict[str, Any]:
    policy = require_market_source_policy(source_id)
    item_limit = min(max_items, policy.max_items)

    async def fetch(url: str, maximum_bytes: int) -> MarketFetchResponse:
        if source_id == "gdelt_doc_metadata_v2":
            with LeadStore(database_path()) as store:
                retry_after = active_provider_cooldown(store.get_free_signal_cache, GDELT_HOST)
            if retry_after:
                raise MarketPayloadError(
                    f"GDELT requests deferred after HTTP 429 until {retry_after}"
                )
        response, body = await _bounded_get(
            client,
            url,
            max_bytes=min(maximum_bytes, policy.max_response_bytes),
            max_redirects=3,
            accept=(
                "application/atom+xml,application/rss+xml,application/xml,text/xml,"
                "application/json,text/plain;q=0.5"
            ),
            allowed_content_types=("atom", "rss", "xml", "json", "text/plain"),
        )
        if source_id == "gdelt_doc_metadata_v2" and response.status_code == 429:
            with LeadStore(database_path()) as store:
                record_provider_cooldown(
                    store.set_free_signal_cache, GDELT_HOST,
                    retry_after_timestamp(response.headers),
                )
        return MarketFetchResponse(
            url=str(response.url),
            content=body,
            status_code=response.status_code,
            mime_type=response.headers.get("content-type", ""),
            headers=dict(response.headers),
        )

    result = await poll_market_source(
        source_id,
        fetch,
        endpoint_urls=_market_endpoint_urls(
            source_id,
            lookback_days=lookback_days,
            max_items=item_limit,
        ),
        limits=MarketFeedLimits(
            max_response_bytes=policy.max_response_bytes,
            max_items=item_limit,
        ),
    )
    batch = result.to_store_batch()
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    retained: list[dict[str, Any]] = []
    for document in batch.get("documents", []):
        published = document.get("published_at") or document.get("source_updated_at")
        if published:
            try:
                parsed = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if parsed.astimezone(UTC) < cutoff:
                    continue
            except ValueError:
                pass
        retained.append(document)
    batch["documents"] = retained[:item_limit]
    batch["promotable_domains"] = list(result.promotable_domains)
    return batch


def _market_candidates_from_batch(
    source_id: str,
    batch: dict[str, Any],
) -> list[DiscoveredHost]:
    candidates: dict[str, DiscoveredHost] = {}
    for document in batch.get("documents", []):
        source_url = str(document.get("canonical_url") or "")
        external_id = str(document.get("external_id") or "")
        title = re.sub(r"^Show HN:\s*", "", str(document.get("title") or ""), flags=re.I)
        first_seen = None
        published_at = document.get("published_at")
        if published_at:
            try:
                first_seen = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            except ValueError:
                first_seen = None
        for mention in document.get("mentions", []):
            if not bool(mention.get("promotable")):
                continue
            domain = (
                str(mention.get("candidate_domain") or mention.get("domain") or "")
                .strip()
                .lower()
                .rstrip(".")
            )
            if not domain:
                continue
            try:
                domain = normalize_apex(domain)
            except ValueError:
                continue
            candidate = DiscoveredHost(
                source_apex=source_id,
                hostname=domain,
                first_seen=first_seen,
                discovery_rank=0.55,
                label_hint=title[:300],
                discovery_evidence_urls=[source_url] if source_url else [],
                provider_record_ids=[external_id] if external_id else [],
            )
            existing = candidates.get(domain)
            if existing is None:
                candidates[domain] = candidate
            else:
                candidates[domain] = existing.model_copy(
                    update={
                        "discovery_evidence_urls": list(
                            dict.fromkeys(
                                [
                                    *existing.discovery_evidence_urls,
                                    *candidate.discovery_evidence_urls,
                                ]
                            )
                        )[:20],
                        "provider_record_ids": list(
                            dict.fromkeys(
                                [
                                    *existing.provider_record_ids,
                                    *candidate.provider_record_ids,
                                ]
                            )
                        )[:20],
                    }
                )
    return list(candidates.values())


def _market_source_reuse_seconds(
    source_id: str,
    cursor: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> int:
    """Return the remaining policy cadence; zero means the source is due."""

    if not cursor or not cursor.get("last_success_at"):
        return 0
    policy = require_market_source_policy(source_id)
    try:
        last_success = datetime.fromisoformat(str(cursor["last_success_at"]).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    due_at = last_success.astimezone(UTC) + timedelta(seconds=policy.minimum_poll_seconds)
    return max(0, int((due_at - reference).total_seconds()))


def _market_company_projection_batch(
    batch: dict[str, Any],
    company_batch: dict[str, Any],
) -> dict[str, Any] | None:
    """Filter the parser payload to evidence safely resolved to exactly one company."""

    source_external_ids = {
        str(value) for value in company_batch.get("source_external_ids", []) if value
    }
    mention_keys = {str(value) for value in company_batch.get("mention_keys", []) if value}
    event_keys = {str(value) for value in company_batch.get("event_keys", []) if value}
    documents: list[dict[str, Any]] = []
    for raw_document in batch.get("documents", []):
        if not isinstance(raw_document, dict):
            continue
        external_id = next(
            (
                str(raw_document.get(key))
                for key in ("external_id", "document_id", "external_key", "id")
                if raw_document.get(key) not in (None, "")
            ),
            "",
        )
        if external_id not in source_external_ids:
            continue
        resolved_mentions = [
            dict(mention)
            for mention in raw_document.get("mentions", [])
            if isinstance(mention, dict) and str(mention.get("mention_key") or "") in mention_keys
        ]
        resolved_events = [
            dict(event)
            for event in raw_document.get("events", [])
            if isinstance(event, dict)
            and str(event.get("event_key") or event.get("external_id") or "") in event_keys
        ]
        if not resolved_mentions and not resolved_events:
            continue
        documents.append(
            {
                **raw_document,
                "mentions": resolved_mentions,
                "events": resolved_events,
            }
        )
    if not documents:
        return None
    return {**batch, "documents": documents}


def _source_execution_progress(
    totals: dict[str, int],
    *,
    candidates_persisted: int,
) -> dict[str, int]:
    progress = {name: int(totals.get(name, 0)) for name in SOURCE_EXECUTION_COUNTER_FIELDS}
    progress["candidates_persisted"] = max(0, int(candidates_persisted))
    return progress


def _source_activation_blockers(suggestion: SourceSuggestion) -> list[str]:
    blockers = list(source_execution_blockers(suggestion))
    if suggestion.source_kind == "repeatable_search" and not os.getenv(OPENAI_SECRET):
        blockers.append("Add an OpenAI API key in Settings to run this saved search")
    return list(dict.fromkeys(blockers))


def _source_run_result(
    row: dict[str, Any],
    latest_run: dict[str, Any],
) -> dict[str, Any] | None:
    """Return explicit or conservatively inferred item status from one source-method run."""

    suggestion_id = int(row["id"])
    summary = latest_run.get("summary") if isinstance(latest_run.get("summary"), dict) else {}
    results = summary.get("source_results", latest_run.get("source_results"))
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            try:
                result_id = int(result.get("suggestion_id"))
            except (TypeError, ValueError):
                continue
            if result_id == suggestion_id:
                return result

    # Compatibility for runs completed before per-source results were recorded. Enumeration is
    # ordered, and sources_completed counts attempts, so cap-skipped tail items remain due.
    suggestion_ids = [
        int(value)
        for value in latest_run.get("suggestion_ids", [])
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    ]
    try:
        position = suggestion_ids.index(suggestion_id)
    except ValueError:
        return None
    if position >= int(latest_run.get("sources_completed") or 0):
        return {"suggestion_id": suggestion_id, "status": "skipped", "legacy": True}

    run_status = str(latest_run.get("status") or "").lower()
    if run_status == "complete":
        return {"suggestion_id": suggestion_id, "status": "complete", "legacy": True}
    if run_status != "partial":
        return {"suggestion_id": suggestion_id, "status": run_status or "failed", "legacy": True}

    name_prefix = f"{str(row.get('name') or '').strip()}:"
    errors = summary.get("errors") if isinstance(summary, dict) else None
    named_errors = [
        str(error)
        for error in errors or []
        if name_prefix and str(error).startswith(name_prefix)
    ]
    return {
        "suggestion_id": suggestion_id,
        "status": "partial" if named_errors else "complete",
        "errors": named_errors,
        "legacy": True,
    }


def _source_scan_policy(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    ignore_run_id: int | None = None,
) -> dict[str, Any]:
    """Return server-owned due state for one accepted source catalogue row."""

    latest_run = row.get("latest_run") if isinstance(row.get("latest_run"), dict) else None
    if latest_run is None or (
        ignore_run_id is not None and int(latest_run.get("id") or 0) == ignore_run_id
    ):
        return {
            "scan_eligible": True,
            "scan_eligibility_reason": "never_scanned",
            "last_completed_at": None,
            "next_due_at": None,
        }

    latest_status = str(latest_run.get("status") or "").lower()
    if latest_status in {"queued", "running", "cancelling"}:
        return {
            "scan_eligible": False,
            "scan_eligibility_reason": "source_scan_active",
            "last_completed_at": None,
            "next_due_at": None,
        }

    result = _source_run_result(row, latest_run)
    coverage = (result or {}).get("coverage") or {}
    retry_after = None
    if coverage.get("source_url") == row.get("url") and coverage.get("retry_after"):
        try:
            retry_after = datetime.fromisoformat(coverage["retry_after"]).astimezone(UTC)
        except (TypeError, ValueError):
            retry_after = None
    if retry_after and (now or datetime.now(UTC)).astimezone(UTC) < retry_after:
        if result and result.get("status") == "complete" and row.get("source_kind") not in {
            "tenant_apex", "repeatable_search",
        }:
            try:
                finished = datetime.fromisoformat(str(
                    latest_run.get("finished_at") or latest_run.get("updated_at")
                ).replace("Z", "+00:00"))
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=UTC)
                retry_after = max(retry_after, finished + timedelta(days=DIRECTORY_RESCAN_DAYS))
            except (TypeError, ValueError):
                pass
        return {"scan_eligible": False, "scan_eligibility_reason": "directory_retry_backoff",
                "last_completed_at": None, "next_due_at": retry_after.isoformat(),
                "coverage_complete": False}
    if not result or str(result.get("status") or "").lower() != "complete":
        return {
            "scan_eligible": True,
            "scan_eligibility_reason": str((result or {}).get("status") or "retry_due"),
            "last_completed_at": None,
            "next_due_at": None,
        }

    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    directory = row.get("source_kind") not in {"tenant_apex", "repeatable_search"}
    # A successful bounded slice is still a scan: pagination and short-lived parsed
    # receipts must not cause repeated directory work during the rescan cooldown.
    if directory and (not coverage or coverage.get("source_url") == row.get("url")):
        completed_value = latest_run.get("finished_at") or latest_run.get("updated_at")
        try:
            scanned_at = datetime.fromisoformat(str(completed_value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            scanned_at = None
        if scanned_at is not None:
            if scanned_at.tzinfo is None:
                scanned_at = scanned_at.replace(tzinfo=UTC)
            scanned_at = scanned_at.astimezone(UTC)
            rescan_at = scanned_at + timedelta(days=DIRECTORY_RESCAN_DAYS)
            if (now or datetime.now(UTC)).astimezone(UTC) < rescan_at:
                return {
                    "scan_eligible": False,
                    "scan_eligibility_reason": "directory_rescan_cooldown",
                    "last_completed_at": scanned_at.isoformat(),
                    "next_due_at": rescan_at.isoformat(),
                    "coverage_complete": bool(coverage.get("complete")),
                }
    if directory and (not coverage or coverage.get("source_url") != row.get("url")):
        return {"scan_eligible": True, "scan_eligibility_reason": "directory_coverage_unknown",
                "last_completed_at": None, "next_due_at": None, "coverage_complete": False}
    if directory and coverage.get("continuation"):
        return {"scan_eligible": True, "scan_eligibility_reason": "directory_continuation_due",
                "last_completed_at": None, "next_due_at": None, "coverage_complete": False}

    completed_value = latest_run.get("finished_at") or latest_run.get("updated_at")
    try:
        completed_at = datetime.fromisoformat(str(completed_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {
            "scan_eligible": True,
            "scan_eligibility_reason": "completion_time_unknown",
            "last_completed_at": None,
            "next_due_at": None,
        }
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    completed_at = completed_at.astimezone(UTC)
    next_due = completed_at + timedelta(hours=SOURCE_METHOD_FRESH_HOURS)
    if directory and coverage.get("fresh_until"):
        try:
            next_due = min(
                next_due, datetime.fromisoformat(coverage["fresh_until"]).astimezone(UTC)
            )
        except (TypeError, ValueError):
            return {
                "scan_eligible": True,
                "scan_eligibility_reason": "coverage_time_unknown",
                "last_completed_at": None,
                "next_due_at": None,
            }
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "scan_eligible": reference >= next_due,
        "coverage_complete": bool(coverage.get("complete")),
        "scan_eligibility_reason": (
            ("fresh_directory_scope" if directory else "fresh_success")
            if reference < next_due else "freshness_expired"
        ),
        "last_completed_at": completed_at.isoformat(),
        "next_due_at": next_due.isoformat(),
    }


def _reconcile_source_request(
    request: DiscoveryMethodRunRequest,
    *,
    ignore_run_id: int | None = None,
) -> list[dict[str, Any]]:
    """Remove fresh/active accepted sources and return compact reuse metadata."""

    if request.method in SERVER_OWNED_DISCOVERY_METHODS:
        return []
    with LeadStore(database_path()) as store:
        by_id = {
            int(row["id"]): row
            for row in store.list_accepted_discovery_sources(
                suggestion_id=(request.suggestion_ids[0]
                               if len(request.suggestion_ids) == 1 else None)
            )
            if int(row["id"]) in request.suggestion_ids
        }
    due_ids: list[int] = []
    omitted: list[dict[str, Any]] = []
    for suggestion_id in request.suggestion_ids:
        row = by_id.get(suggestion_id)
        if row is None:
            due_ids.append(suggestion_id)
            continue
        policy = _source_scan_policy(row, ignore_run_id=ignore_run_id)
        if policy["scan_eligible"]:
            due_ids.append(suggestion_id)
            continue
        omitted.append(
            {
                "suggestion_id": suggestion_id,
                "name": str(row.get("name") or suggestion_id),
                "reason": str(policy["scan_eligibility_reason"]),
                "next_due_at": policy.get("next_due_at"),
            }
        )
    request.suggestion_ids = due_ids
    return omitted


def _automatic_source_request(
    suggestion_id: int,
    suggestion: SourceSuggestion,
) -> DiscoveryMethodRunRequest:
    method = _method_for_suggestion(suggestion)
    if method is None:
        raise ValueError(f"No executable method for {suggestion.source_kind}")
    openai_ready = bool(os.getenv(OPENAI_SECRET))
    return DiscoveryMethodRunRequest(
        method=method,
        suggestion_ids=[suggestion_id],
        max_pages=AUTO_SOURCE_MAX_PAGES,
        max_candidates=AUTO_SOURCE_MAX_CANDIDATES,
        max_network_inspections=AUTO_SOURCE_MAX_NETWORK_INSPECTIONS,
        ai_resolution=openai_ready,
        max_ai_tool_calls=AUTO_SOURCE_MAX_AI_TOOL_CALLS,
        max_ai_reviews=0,
        max_ai_web_reviews=0,
    )


# Market collectors can fetch independently; expensive SQLite publication has one owner.
_MARKET_DATABASE_LOCK = Lock()


async def _run_market_database_work(
    operation: Callable[[], Any], *, serialize: bool = True,
) -> Any:
    def publish():
        if not serialize:
            with source_time("database_operation"):
                return operation()
        with source_time("database_owner_wait"):
            _MARKET_DATABASE_LOCK.acquire()
        try:
            with source_time("database_operation"):
                return operation()
        finally:
            _MARKET_DATABASE_LOCK.release()

    with source_time("database_dispatch"):
        return await _wait_market_database_worker(publish)


async def _wait_market_database_worker(publish):
    worker = asyncio.create_task(asyncio.to_thread(publish))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. Keep ownership until their transaction settles,
        # including a second cancellation during Stop/shutdown.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _market_store_call(method, *args, **kwargs):
    with LeadStore(database_path()) as store:
        return getattr(store, method)(*args, **kwargs)


def _publish_market_feed_batch(source_id, batch, run_id):
    with source_time("market_candidate_extraction"):
        discovered = _market_candidates_from_batch(source_id, batch)
    projected_ids = set()
    reprojected_ids = set()
    executions = reused = 0
    with LeadStore(database_path()) as store:
        with source_time("market_evidence_write"):
            if discovered:
                store.upsert_derived_candidates(discovered)
            persistence = store.persist_market_batch(
                source_id, batch, cursor=batch.get("cursor"), polled_at=datetime.now(UTC),
            )
        with source_time("market_company_projection"):
            for company_batch in persistence.get("company_batches", []):
                projection_batch = _market_company_projection_batch(batch, company_batch)
                candidate_ids = [int(value) for value in company_batch.get("candidate_ids", [])]
                if projection_batch is None or not candidate_ids:
                    continue
                company_key = str(company_batch["company_key"])
                projection = project_market_batch(projection_batch, company_key=company_key)
                recorded = store.record_market_projection(
                    candidate_ids[0], company_key, source_id, projection, source_run_id=run_id,
                )
                projected_ids.update(candidate_ids)
                executions += int(not recorded.get("reused"))
                reused += int(bool(recorded.get("reused")))
                reprojected_ids.update(reproject_candidates(store, candidate_ids))
    return discovered, persistence, projected_ids, reprojected_ids, executions, reused


class DiscoveryMethodManager:
    """Execute accepted public-source methods, then hydrate their exact company hosts."""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.run_id: int | None = None
        self.scan_job_id: str | None = None
        self.state: dict[str, object] = {"status": "idle", "message": "Ready"}

    running = ScanManager.running

    def _adopt_finished_run(self, run_id: int) -> bool:
        """Reuse a terminal result won by another recovery/finalization path."""

        with LeadStore(database_path()) as store:
            persisted = store.get_source_execution_run(run_id)
        if not persisted or not persisted.get("finished_at"):
            return False
        status = str(persisted.get("status") or "complete")
        self.state = {
            **self.state,
            **dict(persisted.get("progress") or {}),
            **dict(persisted.get("summary") or {}),
            "status": status,
            "phase": status,
            "message": (
                "Source work was already complete; reused its terminal result"
                if status in {"complete", "partial"}
                else str(persisted.get("error") or f"Source work already {status}")
            ),
            "finished_at": persisted.get("finished_at"),
            "terminal_result_reused": True,
        }
        return True

    def _selected_suggestions(
        self,
        request: DiscoveryMethodRunRequest,
    ) -> list[SourceSuggestion]:
        if request.method in SERVER_OWNED_DISCOVERY_METHODS:
            return []
        with LeadStore(database_path()) as store:
            requested_ids = list(request.suggestion_ids)
            resolved_by_request = {
                suggestion_id: store.resolve_source_suggestion_id(suggestion_id)
                for suggestion_id in requested_ids
            }
            missing = sorted(
                suggestion_id
                for suggestion_id, canonical_id in resolved_by_request.items()
                if canonical_id is None
            )
            request.suggestion_ids = list(
                dict.fromkeys(
                    int(canonical_id)
                    for canonical_id in resolved_by_request.values()
                    if canonical_id is not None
                )
            )
            by_id = {
                int(row["id"]): row
                for row in store.list_source_suggestions(suggestion_ids=request.suggestion_ids)
                if int(row["id"]) in request.suggestion_ids
            }
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Source suggestions not found: {', '.join(map(str, missing))}",
            )
        not_accepted = sorted(
            suggestion_id for suggestion_id, row in by_id.items() if row.get("status") != "accepted"
        )
        if not_accepted:
            raise HTTPException(
                status_code=409,
                detail=("Accept sources before running them: " + ", ".join(map(str, not_accepted))),
            )
        suggestions = [_source_suggestion(by_id[item]) for item in request.suggestion_ids]
        incompatible = [
            suggestion.name
            for suggestion in suggestions
            if not _method_accepts_suggestion(request.method, suggestion)
        ]
        if incompatible:
            raise HTTPException(
                status_code=422,
                detail=f"Sources do not belong to {request.method}: {', '.join(incompatible)}",
            )
        return suggestions

    def validate(
        self,
        request: DiscoveryMethodRunRequest,
        *,
        ignore_run_id: int | None = None,
    ) -> list[SourceSuggestion]:
        if request.method not in EXECUTABLE_DISCOVERY_METHODS:
            raise HTTPException(status_code=422, detail="Unknown discovery method")
        if self.running:
            raise HTTPException(status_code=409, detail="A public-source scan is already running")
        if request.method == "searches" and not os.getenv(OPENAI_SECRET):
            raise HTTPException(
                status_code=422,
                detail="Repeatable searches require an OpenAI API key in Settings",
            )
        _reconcile_source_request(request, ignore_run_id=ignore_run_id)
        suggestions = self._selected_suggestions(request)
        blocked = [
            (suggestion.name, _source_activation_blockers(suggestion)) for suggestion in suggestions
        ]
        blocked = [(name, reasons) for name, reasons in blocked if reasons]
        if blocked:
            name, reasons = blocked[0]
            raise HTTPException(
                status_code=422,
                detail=f"{name} cannot run: {reasons[0]}",
            )
        return suggestions

    async def start(
        self,
        request: DiscoveryMethodRunRequest,
        *,
        queued_run_id: int | None = None,
    ) -> dict[str, object]:
        omitted_sources = await asyncio.to_thread(
            _reconcile_source_request, request, ignore_run_id=queued_run_id
        )
        suggestions = await asyncio.to_thread(
            self.validate, request, ignore_run_id=queued_run_id
        )
        if request.method not in SERVER_OWNED_DISCOVERY_METHODS and not suggestions:
            self.state = {
                "status": "complete",
                "phase": "complete",
                "message": "All requested source work is already fresh",
                "method": request.method,
                "sources_total": 0,
                "sources_completed": 0,
                "sources_reused_without_network": len(omitted_sources),
                "omitted_sources": omitted_sources,
            }
            return dict(self.state)
        job_id = uuid.uuid4().hex[:12]
        source_kinds = sorted({item.source_kind for item in suggestions})
        source_kind = ",".join(source_kinds) or request.method
        source_total = _discovery_source_total(request, suggestions)
        created_run_id = None

        def create_run():
            nonlocal job_id, created_run_id
            with LeadStore(database_path()) as store:
                if queued_run_id is not None:
                    queued = store.get_source_execution_run(queued_run_id)
                    if queued is None:
                        raise HTTPException(status_code=404, detail="Queued source run not found")
                    queued_suggestion_ids = store.canonical_source_suggestion_ids(
                        list(queued.get("suggestion_ids") or [])
                    )
                    if (
                        queued.get("status") != "queued"
                        or queued.get("method") != request.method
                        or queued_suggestion_ids != request.suggestion_ids
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Queued source run no longer matches this activation",
                        )
                    claimed = store.claim_queued_source_execution_run(queued_run_id)
                    if claimed is None:
                        raise HTTPException(
                            status_code=409,
                            detail="Queued source run is already claimed",
                        )
                    run_id = queued_run_id
                    job_id = str(claimed.get("job_id") or job_id)
                else:
                    run_id = store.start_source_execution_run(
                        request.method,
                        source_kind,
                        request.suggestion_ids,
                        request.model_dump(mode="json"),
                        job_id=job_id,
                    )
                store.update_source_execution_run(
                    run_id,
                    phase="enumerating",
                    sources_total=source_total,
                )
            created_run_id = run_id
            return run_id

        try:
            run_id = await _run_market_database_work(create_run)
        except asyncio.CancelledError:
            if created_run_id is not None:
                await _run_market_database_work(partial(
                    _market_store_call, "finish_source_execution_run", created_run_id,
                    "cancelled", {"cancelled": True}, error="source_start_cancelled",
                ))
            raise
        self.run_id = run_id
        self.scan_job_id = None
        self.state = {
            "status": "running",
            "phase": "enumerating",
            "message": f"Running {str(_method_tab(request.method)['label']).lower()}",
            "job_id": job_id,
            "run_id": run_id,
            "method": request.method,
            "scanner_ids": list(request.scanner_ids),
            "request": request.model_dump(mode="json"),
            "sources_total": source_total,
            "sources_completed": 0,
            "candidates_persisted": 0,
            "candidates_hydrated": 0,
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.task = asyncio.create_task(self._run(request, suggestions, run_id))
        return dict(self.state)

    async def _run(
        self,
        request: DiscoveryMethodRunRequest,
        suggestions: list[SourceSuggestion],
        run_id: int,
    ) -> None:
        warnings: list[str] = []
        errors: list[str] = []
        candidates_by_host: dict[str, DiscoveredHost] = {}
        projected_candidate_ids: set[int] = set()
        reprojected_candidate_ids: set[int] = set()
        totals = {
            "sources_completed": 0,
            "pages_fetched": 0,
            "documents_fetched": 0,
            "direct_candidates": 0,
            "ai_candidates": 0,
            "direct_calls": 0,
            "ai_calls": 0,
            "ai_tool_calls": 0,
            "microlink_attempted": 0,
            "microlink_recovered": 0,
        }
        source_results: list[dict[str, Any]] = []
        try:
            if request.method == "public_graph" and not request.hydrate_companies:
                async with httpx.AsyncClient(
                    timeout=30,
                    headers={"User-Agent": USER_AGENT},
                    trust_env=False,
                ) as client:
                    def cache_get(host: str, kind: str):
                        with LeadStore(database_path()) as store:
                            return store.get_free_signal_cache(host, kind)

                    def cache_set(host: str, kind: str, payload, ttl):
                        with LeadStore(database_path()) as store:
                            store.set_free_signal_cache(host, kind, payload, ttl)

                    graph = await discover_agencies(
                        client,
                        rotation=max(run_id - 1, 0),
                        cache_get=cache_get,
                        cache_set=cache_set,
                    )
                await _run_market_database_work(partial(
                    _market_store_call, "upsert_derived_candidates", graph.candidates,
                ))
                candidates_by_host.update(
                    (candidate.hostname, candidate) for candidate in graph.candidates
                )
                totals.update({
                    "sources_completed": 2,
                    "direct_calls": graph.network_calls,
                    "direct_candidates": len(graph.candidates),
                    "candidates_persisted": len(graph.candidates),
                    "cache_hits": graph.cache_hits,
                    "osm_metro": graph.osm_metro,
                    "wikidata_page": graph.wikidata_page,
                })
                errors.extend(graph.errors)
                await _run_market_database_work(partial(
                    _market_store_call, "update_source_execution_run", run_id,
                    **_source_execution_progress(
                        totals, candidates_persisted=len(candidates_by_host),
                    ),
                ))
                self.state.update(totals)
            if request.method == "company_intelligence":
                with LeadStore(database_path()) as store:
                    prospects = store.list_prospects(
                        min_score=0,
                        live=True,
                        excluded=False,
                        sort="priority",
                        limit=request.max_candidates,
                    )["items"]
                    hosts = [str(item["hostname"]) for item in prospects]
                    existing = store.candidates_by_hosts(hosts)
                candidates_by_host = {item.hostname: item for item in existing}
                totals.update(
                    {
                        "sources_completed": len(request.scanner_ids),
                        "direct_candidates": len(candidates_by_host),
                        "candidates_persisted": len(candidates_by_host),
                    }
                )
                progress = {**totals, "candidates_persisted": len(candidates_by_host)}
                with LeadStore(database_path()) as store:
                    store.update_source_execution_run(
                        run_id,
                        **_source_execution_progress(
                            totals,
                            candidates_persisted=len(candidates_by_host),
                        ),
                    )
                self.state.update(progress)

            if request.method not in SERVER_OWNED_DISCOVERY_METHODS:
                ai_available = bool(os.getenv(OPENAI_SECRET))
                allow_ai = request.ai_resolution and ai_available
                ai_model = source_scout_manager._model() if allow_ai else None
                microlink_api_key = (
                    os.getenv(MICROLINK_SECRET, "").strip() if request.microlink_enabled else ""
                )
                timeout = httpx.Timeout(20.0, connect=8.0)
                limits_http = httpx.Limits(max_connections=10, max_keepalive_connections=5)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits_http,
                    headers={"User-Agent": USER_AGENT},
                    trust_env=False,
                ) as client:
                    for suggestion_id, suggestion in zip(
                        request.suggestion_ids,
                        suggestions,
                        strict=True,
                    ):
                        remaining = request.max_candidates - len(candidates_by_host)
                        if remaining <= 0:
                            warning = (
                                "Global candidate cap reached; remaining sources were not fetched"
                            )
                            if warning not in warnings:
                                warnings.append(warning)
                            source_results.append(
                                {
                                    "suggestion_id": suggestion_id,
                                    "status": "skipped",
                                    "candidates_found": 0,
                                    "errors": ["global_candidate_cap_reached"],
                                }
                            )
                            continue
                        limits = SourceDiscoveryLimits(
                            max_results=remaining,
                            max_pages=request.max_pages,
                            max_tool_calls=request.max_ai_tool_calls,
                            max_microlink_pages=min(
                                SOURCE_MICROLINK_MAX_PAGES,
                                request.max_pages,
                            ),
                        )
                        linked_candidates: list[DiscoveredHost] = []
                        source_errors: list[str] = []
                        source_microlink_attempted = 0
                        source_microlink_recovered = 0
                        source_coverage = {}
                        enumeration_started = asyncio.get_running_loop().time()

                        def source_progress(progress, suggestion=suggestion):
                            self.state.update({
                                "message": f"Reading source · {suggestion.name}",
                                "source_page_progress": {**progress, "name": suggestion.name},
                            })

                        try:
                            page_cache = (
                                await _run_market_database_work(partial(
                                    SourceCrawlCache, database_path(), suggestion_id,
                                    suggestion, limits, write_work=_run_market_database_work,
                                ))
                                if suggestion.source_kind
                                not in {"tenant_apex", "repeatable_search"}
                                else None
                            )
                            with source_time("directory_enumeration"):
                                result = await discover_source(
                                    client,
                                    suggestion,
                                    limits=limits,
                                    ai_model=ai_model,
                                    allow_ai=allow_ai,
                                    microlink_api_key=microlink_api_key,
                                    page_cache=page_cache,
                                    progress=source_progress,
                                )
                            source_coverage = {**result.coverage, "source_url": suggestion.url}
                            if page_cache and page_cache.fresh_until:
                                source_coverage["fresh_until"] = page_cache.fresh_until.isoformat()
                            source_microlink_attempted = result.counts.microlink_attempted
                            source_microlink_recovered = result.counts.microlink_recovered
                            totals["microlink_attempted"] += source_microlink_attempted
                            totals["microlink_recovered"] += source_microlink_recovered
                            totals["pages_fetched"] += result.counts.html_pages
                            totals["documents_fetched"] += result.counts.pdf_files
                            totals["direct_candidates"] += (
                                result.counts.direct_candidates + result.counts.ct_candidates
                            )
                            totals["ai_candidates"] += result.counts.ai_candidates
                            totals["direct_calls"] += (
                                result.counts.ct_calls
                                + result.counts.api_calls
                                + result.counts.html_pages
                                + result.counts.pdf_files
                            )
                            totals["ai_calls"] += int(result.tool_calls > 0)
                            totals["ai_tool_calls"] += result.tool_calls
                            warnings.extend(
                                f"{suggestion.name}: {message}" for message in result.warnings
                            )
                            source_errors = [str(message) for message in result.errors]
                            errors.extend(
                                f"{suggestion.name}: {message}" for message in source_errors
                            )
                            new_hosts: set[str] = set()
                            for candidate in result.candidates:
                                known_host = (
                                    candidate.hostname in candidates_by_host
                                    or candidate.hostname in new_hosts
                                )
                                if not known_host and len(new_hosts) >= remaining:
                                    continue
                                linked_candidates.append(candidate)
                                if candidate.hostname not in candidates_by_host:
                                    new_hosts.add(candidate.hostname)
                            await _run_market_database_work(partial(
                                _market_store_call, "link_source_candidates",
                                run_id, suggestion_id, linked_candidates,
                            ))
                            for candidate in linked_candidates:
                                candidates_by_host.setdefault(candidate.hostname, candidate)
                            if metrics := SOURCE.get():
                                metrics.result.setdefault("directories", {})[suggestion_id] = {
                                    "pages_fetched": result.counts.html_pages,
                                    "documents_fetched": result.counts.pdf_files,
                                    "candidates_linked": len(linked_candidates),
                                    "pages_reused": source_coverage.get("pages_reused"),
                                    "continuation": source_coverage.get("continuation"),
                                    "fetch_attempts": source_coverage.get("fetch_attempts"),
                                }
                        except Exception as exc:
                            logger.exception(
                                "source execution failed run_id=%d source=%s",
                                run_id,
                                suggestion.source_key,
                            )
                            source_errors = [f"{type(exc).__name__}: {exc}"]
                            errors.append(f"{suggestion.name}: {source_errors[0]}")
                        source_results.append(
                            {
                                "suggestion_id": suggestion_id,
                                "status": (
                                    "partial"
                                    if source_errors and linked_candidates
                                    else "failed"
                                    if source_errors
                                    else "complete"
                                ),
                                "candidates_found": len(linked_candidates),
                                "coverage": source_coverage,
                                "enumeration_seconds": round(
                                    asyncio.get_running_loop().time() - enumeration_started, 3),
                                "microlink_attempted": source_microlink_attempted,
                                "microlink_recovered": source_microlink_recovered,
                                "errors": source_errors[:5],
                            }
                        )
                        totals["sources_completed"] += 1
                        progress = {
                            **totals,
                            "candidates_persisted": len(candidates_by_host),
                        }
                        await _run_market_database_work(partial(
                            _market_store_call, "update_source_execution_run", run_id,
                            summary={"source_results": source_results},
                            **_source_execution_progress(
                                totals, candidates_persisted=len(candidates_by_host),
                            ),
                        ))
                        self.state.update(progress)

            candidate_hosts = list(candidates_by_host)
            if request.hydrate_companies and (request.method == "public_graph" or candidate_hosts):
                self.state.update(
                    {
                        "phase": "hydrating",
                        "message": "Hydrating discovered company websites",
                        "candidates_persisted": len(candidates_by_host),
                    }
                )
                with LeadStore(database_path()) as store:
                    store.update_source_execution_run(
                        run_id,
                        phase="hydrating",
                        candidates_persisted=len(candidates_by_host),
                    )
                ai_available = bool(os.getenv(OPENAI_SECRET))
                scan_request = ScanRequest(
                    people_hydration_enabled=request.people_hydration_enabled,
                    microlink_enabled=request.microlink_enabled,
                    sources=[],
                    only_hosts=candidate_hosts or None,
                    max_hosts_per_source=max(1, request.max_candidates),
                    max_network_inspections=request.max_network_inspections,
                    adaptive_refresh=True,
                    concurrency=20,
                    pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
                    english_only=True,
                    free_intelligence=True,
                    max_sitemaps_per_site=4,
                    max_common_crawl_lookups=(
                        0
                        if request.method == "company_intelligence"
                        else min(20, request.max_network_inspections)
                    ),
                    max_news_lookups=(
                        0
                        if request.method == "company_intelligence"
                        else min(5, request.max_network_inspections)
                    ),
                    max_frontier_scans=(
                        min(
                            500,
                            request.max_network_inspections * max(1, len(request.scanner_ids)),
                        )
                        if request.method == "company_intelligence"
                        else min(20, request.max_network_inspections)
                    ),
                    frontier_scanner_ids=(
                        request.scanner_ids if request.method == "company_intelligence" else []
                    ),
                    agency_discovery=request.method == "public_graph",
                    ai=(
                        request.method != "company_intelligence"
                        and ai_available
                        and request.max_ai_reviews > 0
                    ),
                    ai_web=(
                        request.method != "company_intelligence"
                        and ai_available
                        and request.max_ai_web_reviews > 0
                    ),
                    max_ai_calls=(
                        0 if request.method == "company_intelligence" else request.max_ai_reviews
                    ),
                    max_ai_web_calls=(
                        0
                        if request.method == "company_intelligence"
                        else request.max_ai_web_reviews
                    ),
                    evergreen_discovery=False,
                )
                child = await scan_manager.start(
                    scan_request,
                    [],
                    request.max_network_inspections,
                    include_people_hydration=(
                                    request.people_hydration_enabled
                                    and not smart_scan_manager.active
                                ),
                )
                self.scan_job_id = str(child["job_id"])
                self.state["scan_job_id"] = self.scan_job_id
                with LeadStore(database_path()) as store:
                    store.update_source_execution_run(
                        run_id,
                        scan_job_id=self.scan_job_id,
                    )
                child_manager = scan_manager.jobs.get(self.scan_job_id)
                if child_manager and child_manager.task:
                    await child_manager.task
                    child_state = dict(child_manager.state)
                else:  # pragma: no cover - the pool retains a new job until its task completes
                    child_state = dict(scan_manager.state)
                scan_run_id = child_state.get("run_id")
                hydrated = 0
                reused = 0
                scan_summary = {}
                if scan_run_id:
                    with LeadStore(database_path()) as store:
                        metadata = store.run_metadata(int(scan_run_id))
                    scan_summary = dict(metadata.get("summary") or {})
                    hydrated = int(scan_summary.get("candidates_network_scanned", 0))
                    reused = int(
                        scan_summary.get(
                            "candidates_reused_within_90d",
                            scan_summary.get(
                                "candidates_reused_within_30d",
                                scan_summary.get(
                                    "candidates_reused_within_24h",
                                    scan_summary.get("candidates_reused_without_network", 0),
                                ),
                            ),
                        )
                    )
                    if request.method == "public_graph":
                        agency = scan_summary.get("agency_discovery", {})
                        totals.update(
                            {
                                "sources_completed": 2,
                                "direct_calls": int(agency.get("network_calls", 0)),
                                "direct_candidates": int(agency.get("new_candidates", 0)),
                                "candidates_persisted": int(agency.get("backlog_candidates", 0)),
                            }
                        )
                totals["candidates_hydrated"] = hydrated
                totals["candidates_reused_without_network"] = reused
                self.state.update(
                    {
                        "scan_run_id": scan_run_id,
                        "candidates_hydrated": hydrated,
                        "candidates_reused_without_network": reused,
                    }
                )
                if child_state.get("status") != "complete":
                    child_gaps = [
                        str(detail)
                        for key in (
                            "free_intelligence",
                            "common_crawl",
                            "news_intelligence",
                            "frontier_intelligence",
                        )
                        for detail in (scan_summary.get(key) or {}).get("errors", [])
                        if detail
                    ]
                    if child_gaps:
                        errors.extend(
                            f"Hydration: {detail}" for detail in dict.fromkeys(child_gaps)
                        )
                    else:
                        errors.append(
                            "Hydration: "
                            f"{int(scan_summary.get('failure_count') or 0):,} website failures; "
                            f"{child_state.get('message') or child_state.get('status')}"
                        )

            # Newly discovered domains have no target snapshot until hydration finishes.
            # Re-run the projection locally afterward; this rewrites only the derived target
            # row and never the saved website observation or either 90-day completion clock.
            if projected_candidate_ids:
                def reproject():
                    with LeadStore(database_path()) as store:
                        return reproject_candidates(
                            store,
                            sorted(projected_candidate_ids),
                        )

                reprojected_candidate_ids.update(await _run_market_database_work(reproject))
            totals["targets_reprojected"] = len(reprojected_candidate_ids)

            persisted_count = (
                int(totals.get("candidates_persisted", 0))
                if request.method == "public_graph"
                else len(candidates_by_host)
            )
            has_output = bool(persisted_count) or bool(totals.get("candidates_hydrated"))
            status = "partial" if errors and has_output else "failed" if errors else "complete"
            summary = {
                **totals,
                "source_results": source_results,
                "candidates_persisted": persisted_count,
                "candidates_hydrated": totals.get("candidates_hydrated", 0),
                "candidates_reused_without_network": totals.get(
                    "candidates_reused_without_network",
                    0,
                ),
                "warnings": warnings[:50],
                "errors": errors[:50],
                "scan_job_id": self.scan_job_id,
                "scan_run_id": self.state.get("scan_run_id"),
                "hydration_deferred": bool(candidate_hosts and not request.hydrate_companies),
            }
            await _run_market_database_work(lambda: _market_store_call(
                "finish_source_execution_run", run_id, status, summary,
                error="; ".join(errors[:5]),
            ))
            self.state = {
                **self.state,
                **summary,
                "error": "; ".join(errors[:5]),
                "status": status,
                "phase": "complete" if status != "failed" else "failed",
                "message": (
                    (
                        (f"{persisted_count} companies found · "
                         "hydration queued for the shared due phase")
                        if summary["hydration_deferred"]
                        else (f"{persisted_count} companies found · "
                              f"{summary['candidates_hydrated']} hydrated")
                    )
                    if status != "failed"
                    else (errors[0] if errors else "Discovery method failed")
                ),
                "finished_at": datetime.now(UTC).isoformat(),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._adopt_finished_run(run_id):
                logger.info(
                    "source execution already finalized; reusing terminal result "
                    "run_id=%d status=%s",
                    run_id,
                    self.state.get("status"),
                )
                return
            logger.exception("discovery method run crashed run_id=%d", run_id)
            error = f"{type(exc).__name__}: {exc}"
            await _run_market_database_work(partial(
                _market_store_call, "finish_source_execution_run", run_id,
                "failed", {**totals, "source_results": source_results},
                error=error,
            ))
            self.state = {
                **self.state,
                "status": "failed",
                "phase": "failed",
                "message": error,
                "error": error,
                "finished_at": datetime.now(UTC).isoformat(),
            }

    def status_payload(self) -> dict[str, object]:
        payload = dict(self.state)
        if self.running and self.scan_job_id:
            child = scan_manager.jobs.get(self.scan_job_id)
            if child:
                payload["hydration"] = dict(child.state)
        return payload

    def active_payloads(self) -> list[dict[str, object]]:
        return [self.status_payload()] if self.running else []

    async def cancel(self, *, reason: str = "source_cancelled") -> dict[str, object] | None:
        """Stop this method run and preserve any work already committed by its child scan."""

        if not self.running or self.run_id is None or self.task is None:
            return None
        normalized_reason = reason.strip()[:4_000] or "source_cancelled"
        self.state.update(
            {
                "status": "cancelling",
                "phase": "cancelling",
                "message": "Stopping source method",
            }
        )
        with LeadStore(database_path()) as store:
            store.update_source_execution_run(
                self.run_id,
                status="cancelling",
                phase="cancelling",
                error=normalized_reason,
            )
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        with LeadStore(database_path()) as store:
            run = store.get_source_execution_run(self.run_id)
            if run and not run.get("finished_at"):
                run = store.finish_source_execution_run(
                    self.run_id,
                    "cancelled",
                    {
                        **dict(run.get("progress") or {}),
                        "cancelled": True,
                        "reason": normalized_reason,
                        "scan_job_id": run.get("scan_job_id"),
                        "scan_run_id": run.get("scan_run_id"),
                    },
                    error=normalized_reason,
                )
        self.state = {
            **self.state,
            "status": "cancelled",
            "phase": "cancelled",
            "message": "Source method cancelled",
            "error": normalized_reason,
            "finished_at": (run or {}).get("finished_at") or datetime.now(UTC).isoformat(),
        }
        return dict(self.state)

    async def shutdown(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.run_id is not None:
            with LeadStore(database_path()) as store:
                run = store.get_source_execution_run(self.run_id)
                if run and run.get("status") in {"running", "cancelling"}:
                    store.requeue_source_execution_run(self.run_id)


class DiscoveryMethodPool:
    """Run bounded method jobs and drain durable accepted-source queues."""

    def __init__(self) -> None:
        self.managers: dict[str, DiscoveryMethodManager] = {}
        self.last_manager: DiscoveryMethodManager | None = None
        self.queue_tasks: dict[str, asyncio.Task[None]] = {}
        self.starting: dict[
            DiscoveryMethodManager, tuple[asyncio.Task, DiscoveryMethodRunRequest]
        ] = {}
        self.stopping = 0

    @property
    def running(self) -> bool:
        return bool(self.starting) or any(manager.running for manager in self.managers.values())

    @property
    def run_id(self) -> int | None:
        return self.last_manager.run_id if self.last_manager else None

    def _selected_suggestions(self, request: DiscoveryMethodRunRequest) -> list[SourceSuggestion]:
        manager = self.managers.get(request.method) or DiscoveryMethodManager()
        return manager._selected_suggestions(request)

    def _prune_completed_company_managers(self) -> None:
        completed = [
            key
            for key, manager in self.managers.items()
            if key.startswith("company_intelligence:") and not manager.running
            and manager not in self.starting
        ]
        for key in completed:
            self.managers.pop(key, None)

    async def start(
        self, request: DiscoveryMethodRunRequest, *, independent_sources: bool = False,
    ) -> dict[str, object]:
        if self.stopping:
            raise HTTPException(status_code=409, detail="Source work is stopping")
        manager_key = request.method
        if independent_sources:
            if (
                len(request.suggestion_ids) != 1
                or request.ai_resolution
                or request.hydrate_companies
                or request.method in SERVER_OWNED_DISCOVERY_METHODS
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Parallel source work requires one free extraction-only source",
                )
            for key in list(self.managers):
                if (key.startswith("source_extraction:") and not self.managers[key].running
                        and self.managers[key] not in self.starting):
                    self.managers.pop(key)
            manager_key = f"source_extraction:{request.method}:{request.suggestion_ids[0]}"
            if any((manager.running or manager in self.starting)
                   and set(request.suggestion_ids).intersection(
                    self.starting[manager][1].suggestion_ids if manager in self.starting else
                    dict(manager.state.get("request") or {}).get("suggestion_ids", []))
                   for manager in self.managers.values()):
                raise HTTPException(
                    status_code=409, detail="This source already has an active scan"
                )
        elif request.method == "company_intelligence":
            self._prune_completed_company_managers()
            active_scanners = {
                str(scanner_id)
                for key, active_manager in self.managers.items()
                if key.startswith("company_intelligence:")
                and (active_manager.running or active_manager in self.starting)
                for scanner_id in (
                    active_manager.status_payload().get("scanner_ids")
                    or dict(active_manager.status_payload().get("request") or {}).get(
                        "scanner_ids", []
                    )
                )
            }
            overlapping = sorted(active_scanners.intersection(request.scanner_ids))
            if overlapping:
                raise HTTPException(
                    status_code=409,
                    detail="Company scanners already running: " + ", ".join(overlapping),
                )
            manager_key = f"company_intelligence:{uuid.uuid4().hex[:12]}"
        else:
            current = self.managers.get(request.method)
            if current and (current.running or current in self.starting):
                label = str((_method_tab(request.method) or {}).get("label") or request.method)
                raise HTTPException(
                    status_code=409,
                    detail=f"A {label} scan is already running",
                )
        manager = DiscoveryMethodManager()
        self.managers[manager_key] = manager
        self.last_manager = manager
        return await self._start_manager(manager, request)

    async def _start_manager(self, manager, request, *, queued_run_id=None):
        # Startup awaits validation/publication before it owns a task. Reserve that interval
        # too: pruning it loses the child and prematurely releases a parent worker slot.
        if self.stopping:
            raise HTTPException(status_code=409, detail="Source work is stopping")
        self.starting[manager] = (asyncio.current_task(), request)
        manager.state = {"status": "starting", "method": request.method,
                         "request": request.model_dump(mode="json")}
        try:
            if queued_run_id is None:
                return await manager.start(request)
            return await manager.start(request, queued_run_id=queued_run_id)
        except BaseException as exc:
            if manager.task is None:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                manager.state.update(status=status, phase=status)
            raise
        finally:
            self.starting.pop(manager, None)

    async def _cancel_startups(self, tasks=None):
        from .async_work import cancel_and_drain

        selected = list(tasks) if tasks is not None else [
            task for task, _ in self.starting.values()
        ]
        await cancel_and_drain([task for task in selected if task is not asyncio.current_task()])

    async def enqueue(self, request: DiscoveryMethodRunRequest) -> dict[str, object]:
        """Persist one accepted source for serial execution by its typed method."""

        if len(request.suggestion_ids) != 1:
            raise HTTPException(
                status_code=422,
                detail="Automatic source activation queues exactly one source per run",
            )
        validator = DiscoveryMethodManager()
        suggestions = await asyncio.to_thread(validator.validate, request)
        if not suggestions:
            return {
                "status": "complete",
                "phase": "complete",
                "activation_state": "already_active",
                "message": "Source work is already fresh",
                "suggestion_ids": [],
            }
        suggestion = suggestions[0]
        job_id = uuid.uuid4().hex[:12]
        def persist_queue():
            with LeadStore(database_path()) as store:
                queued = store.queue_source_execution_run(
                    request.method,
                    suggestion.source_kind,
                    request.suggestion_ids[0],
                    request.model_dump(mode="json"),
                    job_id=job_id,
                )
            return queued

        queued = await asyncio.to_thread(persist_queue)
        queued_method = str(queued.get("method") or request.method)
        self._ensure_queue_worker(queued_method)
        activation_state = "running" if queued.get("status") == "running" else "queued"
        return {
            **queued,
            "activation_state": activation_state,
            "message": (
                f"Accepted and {activation_state} in "
                f"{str((_method_tab(queued_method) or {}).get('label') or queued_method)}"
            ),
        }

    def _ensure_queue_worker(self, method: str) -> None:
        task = self.queue_tasks.get(method)
        if task is None or task.done():
            self.queue_tasks[method] = asyncio.create_task(self._drain_queued(method))

    def resume_queued(self) -> int:
        with LeadStore(database_path()) as store:
            queued = store.list_queued_source_execution_runs()
        methods = {str(run["method"]) for run in queued}
        for method in methods:
            self._ensure_queue_worker(method)
        return len(queued)

    async def _drain_queued(self, method: str) -> None:
        try:
            while True:
                while bool(
                    getattr(
                        smart_scan_manager,
                        "active",
                        getattr(smart_scan_manager, "running", False),
                    )
                ):
                    await asyncio.sleep(0.5)
                current = self.managers.get(method)
                if current and current.running and current.task:
                    await current.task
                with LeadStore(database_path()) as store:
                    queued = store.list_queued_source_execution_runs(method)
                if not queued:
                    return
                run = queued[0]
                try:
                    request = DiscoveryMethodRunRequest.model_validate(run["request"])
                    manager = DiscoveryMethodManager()
                    self.managers[method] = manager
                    self.last_manager = manager
                    await self._start_manager(manager, request, queued_run_id=int(run["id"]))
                except HTTPException as exc:
                    with LeadStore(database_path()) as store:
                        current_run = store.get_source_execution_run(int(run["id"]))
                    if current_run and current_run.get("status") in {
                        "running",
                        "cancelling",
                    }:
                        logger.info(
                            "source activation claimed by another worker run_id=%s",
                            run.get("id"),
                        )
                        return
                    # Only a missing, later-restorable capability should park a queue head.
                    # Permanent 422 validation failures must not starve valid work behind it.
                    retryable_capability = (
                        exc.status_code == 422
                        and request.method == "searches"
                        and not os.getenv(OPENAI_SECRET)
                    )
                    if retryable_capability:
                        logger.info(
                            "source activation remains queued run_id=%s detail=%s",
                            run.get("id"),
                            exc.detail,
                        )
                        return
                    if current_run and current_run.get("status") == "queued":
                        with LeadStore(database_path()) as store:
                            store.finish_source_execution_run(
                                int(run["id"]),
                                "failed",
                                dict(run.get("progress") or {}),
                                error=str(exc.detail),
                            )
                    continue
                except (TypeError, ValueError, ValidationError) as exc:
                    with LeadStore(database_path()) as store:
                        store.finish_source_execution_run(
                            int(run["id"]),
                            "failed",
                            dict(run.get("progress") or {}),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    continue
                if manager.task:
                    await manager.task
        finally:
            task = self.queue_tasks.get(method)
            if task is asyncio.current_task():
                self.queue_tasks.pop(method, None)

    def active_payloads(self) -> list[dict[str, object]]:
        return [manager.status_payload() for manager in self.managers.values()
                if manager.running or manager in self.starting]

    def status_payload(self) -> dict[str, object]:
        active_runs = self.active_payloads()
        primary = (
            active_runs[0]
            if active_runs
            else (self.last_manager.status_payload() if self.last_manager else {})
        )
        return {
            **primary,
            "active_run": active_runs[0] if active_runs else None,
            "active_runs": active_runs,
        }

    async def cancel_all(self, *, reason: str = "smart_scan_stopped") -> None:
        self.stopping += 1
        try:
            await self._cancel_all(reason=reason)
        finally:
            self.stopping -= 1

    async def _cancel_all(self, *, reason: str) -> None:
        """Stop queued and active source work without discarding committed evidence."""
        await asyncio.to_thread(self._cancel_queued, reason)
        await self._cancel_startups()
        # Cancel managers before queue waiters: cancelling a waiter can propagate to
        # its child before the child's durable cancellation receipt is written.
        await asyncio.gather(*(manager.cancel(reason=reason)
                               for manager in list(self.managers.values()) if manager.running))
        workers = list(self.queue_tasks.values())
        for task in workers:
            if not task.done():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.queue_tasks.clear()

    def _cancel_queued(self, reason: str) -> None:
        with LeadStore(database_path()) as store:
            for run in store.list_queued_source_execution_runs():
                store.finish_source_execution_run(
                    int(run["id"]), "cancelled", {"cancelled": True, "reason": reason},
                    error=reason,
                )

    async def cancel_source(
        self,
        suggestion_id: int,
        *,
        reason: str = "source_cancelled",
    ) -> list[int]:
        """Cancel active one-source method work containing the canonical suggestion."""

        with LeadStore(database_path()) as store:
            canonical_id = store.resolve_source_suggestion_id(suggestion_id)
            pending = [task for task, request in self.starting.values()
                       if canonical_id is not None and any(
                           store.resolve_source_suggestion_id(member) == canonical_id
                           for member in request.suggestion_ids)]
        if canonical_id is None:
            return []
        await self._cancel_startups(pending)
        cancelled_run_ids: list[int] = []
        cancelled_methods: set[str] = set()
        for manager in list(self.managers.values()):
            request = dict(manager.state.get("request") or {})
            member_ids = [int(value) for value in request.get("suggestion_ids", [])]
            if canonical_id not in member_ids or not manager.running:
                continue
            payload = await manager.cancel(reason=reason)
            if payload is None:
                continue
            cancelled_run_ids.append(int(payload["run_id"]))
            cancelled_methods.add(str(payload.get("method") or ""))
        if cancelled_methods:
            # A durable queue worker awaiting the cancelled run exits with it. Let its
            # cleanup finish, then restart draining any unrelated source queued behind it.
            await asyncio.sleep(0)
            for method in cancelled_methods:
                if method:
                    self._ensure_queue_worker(method)
        return cancelled_run_ids

    async def shutdown(self) -> None:
        await self._cancel_startups()
        pending_workers = [task for task in self.queue_tasks.values() if not task.done()]
        for task in pending_workers:
            task.cancel()
        if pending_workers:
            await asyncio.gather(*pending_workers, return_exceptions=True)
        self.queue_tasks.clear()
        await asyncio.gather(
            *(manager.shutdown() for manager in self.managers.values()),
            return_exceptions=True,
        )


discovery_method_manager = DiscoveryMethodPool()


async def _activate_never_started_accepted_sources(
    *,
    source_kinds: set[str] | None = None,
) -> int:
    """Queue accepted sources that became runnable after a capability change."""

    with LeadStore(database_path()) as store:
        accepted = store.list_accepted_discovery_sources()
    activated = 0
    for row in accepted:
        if source_kinds is not None and str(row.get("source_kind")) not in source_kinds:
            continue
        if row.get("latest_run") is not None:
            continue
        suggestion = _source_suggestion(row)
        if _source_activation_blockers(suggestion):
            continue
        try:
            await discovery_method_manager.enqueue(
                _automatic_source_request(int(row["id"]), suggestion)
            )
        except (HTTPException, TypeError, ValueError, ValidationError) as exc:
            logger.warning(
                "accepted source did not activate after capability change source_id=%s error=%s",
                row.get("id"),
                exc,
            )
            continue
        activated += 1
    return activated


class AdDiscoveryManager:
    """Own short-lived quotes and one manually initiated paid run at a time."""

    def __init__(self) -> None:
        self.quotes: dict[str, dict[str, object]] = {}
        self.task: asyncio.Task[None] | None = None
        self.run_id: int | None = None
        self.cancel_event: asyncio.Event | None = None
        self.active_adapter: object | None = None
        self.active_provider_run_id: str | None = None
        self.state: dict[str, object] = {"status": "idle", "message": "Ready"}

    running = ScanManager.running

    def _prune_quotes(self) -> None:
        now = datetime.now(UTC)
        self.quotes = {
            key: value
            for key, value in self.quotes.items()
            if isinstance(value.get("expires_at"), datetime) and value["expires_at"] > now
        }

    def create_quote(self, request: AdDiscoveryRequest) -> dict[str, object]:
        """Compute and retain a quote without touching Apify or paid-run persistence."""
        self._prune_quotes()
        quote = estimate_ad_scan(request)
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(minutes=AD_QUOTE_TTL_MINUTES)
        self.quotes[quote.quote_id] = {
            "quote": quote,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        return {
            "quote": quote.model_dump(mode="json"),
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "runnable": bool(os.getenv(APIFY_SECRET)),
        }

    def _provider_run_changed(self, adapter: object | None, run_id: str | None) -> None:
        self.active_adapter = adapter
        self.active_provider_run_id = run_id

    async def start(
        self, payload: AdDiscoveryRunStart, *,
        parent_checkpoint: Callable[[int], Awaitable[None]] | None = None,
        seed_candidate_ids: list[int] | None = None,
    ) -> dict[str, object]:
        if self.running:
            raise HTTPException(status_code=409, detail="A paid-ad run is already running")
        if not payload.confirmed:
            raise HTTPException(
                status_code=422,
                detail="Explicit provider-cap approval is required",
            )
        token = os.getenv(APIFY_SECRET, "").strip()
        if not token:
            raise HTTPException(status_code=422, detail="APIFY_API_TOKEN is not configured")
        self._prune_quotes()
        retained = self.quotes.get(payload.quote_id)
        if retained is None:
            raise HTTPException(status_code=409, detail="Quote expired or is no longer available")
        quote = retained.get("quote")
        if not isinstance(quote, AdScanQuote):
            raise HTTPException(status_code=409, detail="Quote is no longer valid")
        if payload.confirmed_max_usd != quote.maximum_charge_usd:
            raise HTTPException(
                status_code=422,
                detail="Confirmed cap must exactly match the estimated provider cap",
            )
        request = quote.request
        with LeadStore(database_path()) as store:
            run_id = store.start_ad_discovery_run(
                provider="apify",
                mode=request.mode,
                request=request.normalized_payload(),
                estimate={
                    **quote.model_dump(mode="json"),
                    "issued_at": retained["issued_at"].isoformat(),
                    "expires_at": retained["expires_at"].isoformat(),
                },
                approved_cap_microusd=_microusd(quote.maximum_charge_usd),
                estimated_cost_microusd=_microusd(quote.estimated_known_cost_usd),
                rate_card_version=AD_RATE_CARD_VERSION,
                job_id=uuid.uuid4().hex[:16],
            )
            run = store.get_ad_discovery_run(run_id)
        self.quotes.pop(payload.quote_id, None)
        self.run_id = run_id
        self.cancel_event = asyncio.Event()
        self.state = {
            "status": "queued",
            "message": "Paid discovery queued",
            "run_id": run_id,
            "run": run or {},
        }
        if parent_checkpoint is not None:
            await parent_checkpoint(run_id)
        self.task = asyncio.create_task(self._run(
            run_id, request, quote, token, seed_candidate_ids=seed_candidate_ids,
        ))
        return self.state

    async def _run(
        self,
        run_id: int,
        request: AdDiscoveryRequest,
        quote: AdScanQuote,
        token: str,
        *, seed_candidate_ids: list[int] | None = None,
    ) -> None:
        executor = AdDiscoveryExecutor(
            database_path(),
            token,
            cancel_event=self.cancel_event,
            active_run_callback=self._provider_run_changed,
            seed_candidate_ids=seed_candidate_ids,
        )
        terminal_status = "complete"
        error = ""
        summary: dict[str, object] = {}
        try:
            self.state = {
                "status": "running",
                "message": "Paid discovery running",
                "run_id": run_id,
            }
            summary = await executor.execute(run_id, request, quote)
            if summary.get("errors"):
                useful_results = any(
                    int(summary.get(key) or 0) > 0
                    for key in (
                        "queries_completed",
                        "advertisers",
                        "creatives",
                        "verified_links",
                        "candidates_promoted",
                    )
                )
                terminal_status = "partial" if useful_results else "failed"
            else:
                terminal_status = "complete"
        except PaidRunCancelled as exc:
            terminal_status = "cancelled"
            error = str(exc)
            summary = {"cancelled": True, "errors": executor.errors[:25]}
        except PaidBudgetExhausted as exc:
            error = str(exc)
            with LeadStore(database_path()) as store:
                current = store.get_ad_discovery_run(run_id) or {}
            progress = current.get("progress") or {}
            terminal_status = (
                "partial"
                if int(progress.get("creatives_found") or 0)
                or int(progress.get("queries_completed") or 0)
                else "failed"
            )
            summary = {"budget_exhausted": True, "errors": [error, *executor.errors[:24]]}
        except asyncio.CancelledError:
            terminal_status = "interrupted"
            error = "Local service stopped; paid work was not resumed"
            summary = {"interrupted": True}
        except Exception as exc:
            terminal_status = "failed"
            error = f"{type(exc).__name__}: {exc}"[:4_000]
            summary = {"errors": [error, *executor.errors[:24]]}
            logger.exception("paid-ad discovery failed run_id=%d", run_id)
        finally:
            self._provider_run_changed(None, None)
            with LeadStore(database_path()) as store:
                finished = store.finish_ad_discovery_run(
                    run_id,
                    terminal_status,
                    summary=summary,
                    error=error,
                    actual_cost_microusd=_microusd(executor.provider_reported_cost),
                )
                # Paid observations are deliberately invisible to scoring while a run is
                # running. Reproject only after the terminal status makes successful or
                # partial evidence authoritative; failed/cancelled runs thereby contribute
                # no score even if they persisted diagnostic rows before stopping.
                affected_candidates = store.ad_observation_candidate_ids(run_id)
            if affected_candidates:
                def reproject():
                    with LeadStore(database_path()) as store:
                        reproject_candidates(store, affected_candidates)

                await asyncio.to_thread(reproject)
            self.state = {
                "status": terminal_status,
                "message": (
                    "Paid discovery complete"
                    if terminal_status == "complete"
                    else "Paid discovery finished with partial results"
                    if terminal_status == "partial"
                    else "Paid discovery cancelled"
                    if terminal_status == "cancelled"
                    else "Paid discovery failed"
                ),
                "run_id": run_id,
                "run": finished or {},
            }

    def status_payload(self) -> dict[str, object]:
        with LeadStore(database_path()) as store:
            run = store.get_ad_discovery_run(self.run_id) if self.run_id else None
            if run is None:
                recent = store.list_ad_discovery_runs(limit=1)
                run = recent[0] if recent else None
        if run:
            return {
                **self.state,
                "status": run["status"],
                "run_id": run["id"],
                "active_provider_run_id": self.active_provider_run_id,
                "run": run,
            }
        return dict(self.state)

    async def cancel(self, run_id: int) -> dict[str, object]:
        if not self.running or self.run_id != run_id or self.cancel_event is None:
            with LeadStore(database_path()) as store:
                run = store.get_ad_discovery_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Paid-ad run not found")
            raise HTTPException(status_code=409, detail="Paid-ad run is not active")
        self.cancel_event.set()
        with LeadStore(database_path()) as store:
            store.update_ad_discovery_run(
                run_id,
                status="cancelling",
                phase="cancelling",
                cancel_requested=True,
            )
        adapter = self.active_adapter
        provider_run_id = self.active_provider_run_id
        if adapter is not None and provider_run_id:
            try:
                await adapter.abort_run(provider_run_id)
            except Exception as exc:
                logger.warning(
                    "provider abort failed run_id=%d provider_run_id=%s error=%s",
                    run_id,
                    provider_run_id,
                    type(exc).__name__,
                )
        return self.status_payload()

    async def shutdown(self) -> None:
        if not self.running or self.run_id is None:
            return
        try:
            await self.cancel(self.run_id)
            if self.task:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=15)
        except (HTTPException, TimeoutError):
            if self.task:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task


ad_discovery_manager = AdDiscoveryManager()


SMART_SCAN_PHASES: tuple[dict[str, str], ...] = (
    {
        "id": "planning",
        "label": "Quantify backlog",
        "description": "Count due sources and companies; send no network requests.",
    },
    {
        "id": "source_research",
        "label": "Find new company sources",
        "description": "Run one bounded discovery pass; queue candidates for review.",
    },
    {
        "id": "source_execution",
        "label": "Query sources",
        "description": "Execute due approved sources, recent PR/news RSS and free public methods.",
    },
    {
        "id": "base_hydration",
        "label": "Refresh companies",
        "description": "Update companies older than 90 days; fill scanner coverage gaps.",
    },
    {
        "id": "microlink_hydration", "label": "Hydrate selected evidence",
        "description": "Run selected Microlink capabilities within company and request limits.",
    },
    {
        "id": "seo_correction",
        "label": "Correct search rank",
        "description": (
            "Reuse completed SEO for 90 days; collect only due stages and update scores."
        ),
    },
    {
        "id": "paid_ads", "label": "Research paid ads",
        "description": "Collect due Meta ads for the selected ranked cohort within its cap.",
    },
    {
        "id": "ai_shortlist",
        "label": "Triage gaps",
        "description": "Rank residual evidence gaps; analyze within the AI call cap.",
    },
    {
        "id": "people_hydration",
        "label": "Hydrate decision-makers",
        "description": (
            "Refresh due top-ranked leadership and marketing/content contacts; hold "
            "completed company checks for 90 days, including empty checks."
        ),
    },
)


def _smart_scan_ai_estimate(
    ai_site_limit: int, *, due_hydration_enabled: bool = True
) -> Decimal:
    site_limit = max(0, min(int(ai_site_limit), SMART_SCAN_MAX_CANDIDATE_AI_CALLS))
    source_cost = (
        Decimal(AI_QUERY_SCOUT_MAXIMUM) / Decimal(1_000_000)
        + (SMART_SCAN_SOURCE_AI_TOOL_CALLS - 5) * SMART_SCAN_SOURCE_AI_ESTIMATE_PER_TOOL_CALL_USD
        if due_hydration_enabled else Decimal("0")
    )
    candidate_cost = site_limit * SMART_SCAN_CANDIDATE_AI_ESTIMATE_PER_SITE_USD
    candidate_web_cost = (
        min(site_limit, SMART_SCAN_CANDIDATE_AI_WEB_CALLS)
        * SMART_SCAN_CANDIDATE_AI_WEB_ESTIMATE_PER_SITE_USD
    )
    return (source_cost + candidate_cost + candidate_web_cost).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _smart_scan_snapshot_presentation(
    item: Mapping[str, object],
    child_phase: str,
) -> tuple[str, str, str]:
    hostname = str(item.get("hostname") or "company")
    if item.get("error") and item.get("observation_current_run") is False:
        return (
            "cached",
            f"Reused {hostname}",
            "Historical observation retained for rescoring; no new network request was made",
        )
    if item.get("error"):
        return (
            "error",
            "A candidate inspection failed",
            f"{hostname}: {item['error']}",
        )
    fetch_outcome = str(item.get("fetch_outcome") or "unknown")
    if fetch_outcome in {
        "platform_blocked",
        "not_public_storefront",
        "retired_hostname",
        "circuit_open",
    }:
        return (
            "skip",
            f"Skipped {hostname}",
            str(item.get("fetch_disposition") or fetch_outcome.replace("_", " ")),
        )
    return (
        "reviewed",
        f"Reviewed {hostname}",
        (
            f"{child_phase.replace('_', ' ')} · score "
            f"{int(item.get('final_score') or 0)}"
        ),
    )


def _smart_scan_dataforseo_limits(site_limit: int) -> tuple[int, int, int]:
    target_limit = max(1, min(int(site_limit), SMART_SCAN_DATAFORSEO_MAX_TARGET_LIMIT))
    detail_limit = (
        target_limit + SMART_SCAN_DATAFORSEO_DETAIL_DIVISOR - 1
    ) // SMART_SCAN_DATAFORSEO_DETAIL_DIVISOR
    bulk_requests = (target_limit + DATAFORSEO_BULK_BATCH_LIMIT - 1) // (
        DATAFORSEO_BULK_BATCH_LIMIT
    )
    return target_limit, detail_limit, bulk_requests + detail_limit


def _smart_scan_dataforseo_estimate(site_limit: int) -> Decimal:
    target_limit, detail_limit, request_cap = _smart_scan_dataforseo_limits(site_limit)
    bulk_requests = request_cap - detail_limit
    estimate = (
        bulk_requests * SMART_SCAN_DATAFORSEO_BULK_TASK_ESTIMATE_USD
        + target_limit * SMART_SCAN_DATAFORSEO_BULK_ITEM_ESTIMATE_USD
        + detail_limit * SMART_SCAN_DATAFORSEO_DETAIL_TASK_ESTIMATE_USD
    )
    return estimate.quantize(Decimal("0.01"))


def _smart_scan_people_estimate(company_limit: int) -> dict:
    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        cohort = store.treg_people_ranking_candidates(
            limit=company_limit, due_only=True, include_target=False,
        )
    due = sum(bool(item.get("people_due", True)) for item in cohort)
    estimate = estimate_people_hydration(
        company_count=due, people_limit=DEFAULT_PEOPLE_LIMIT, email_limit=0,
        email_max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
    )
    return {"selected": len(cohort), "due": due, "reused": len(cohort) - due,
            "estimate_usd": float(estimate["maximum_usd"])}


def _smart_scan_ad_plan(company_limit: int, maximum: Decimal) -> dict:
    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        store.connection.execute("BEGIN")
        cohort = store.treg_people_ranking_candidates(limit=company_limit, include_target=False)
        ids = [int(item["candidate_id"]) for item in cohort]
        seeds = store.select_ad_hydration_seeds(
            limit=company_limit, candidate_ids=ids, lane="all", scope="all", only_due=True,
            include_details=False,
        )
    request = AdDiscoveryRequest(
        mode="hydrate", existing_target_limit=max(1, len(seeds)),
        meta_ads_per_query=20, meta_ads_per_advertiser=20, max_new_advertisers=0,
        hydrate_websites=False, website_hydration_limit=0, max_total_charge_usd=maximum,
    )
    quote = estimate_ad_scan(request)
    return {"selected": len(cohort), "due": len(seeds),
            "reused_or_ineligible": len(cohort) - len(seeds),
            "estimate_usd": float(min(quote.estimated_known_cost_usd, maximum)) if seeds else 0.0,
            "maximum_usd": float(maximum), "request": request,
            "candidate_ids": [int(seed["candidate_id"]) for seed in seeds]}


def _smart_scan_paid_estimate(
    ai_sites: int, seo_sites: int, due_hydration: bool, source_ai: bool | None = None,
    microlink: bool = False, people_sites: int = 0,
    ads_sites: int = 0, ads_max_usd: Decimal = Decimal("5.00"),
) -> dict:
    from .paid_scan_plan import ai_plan, seo_plan

    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        ai = ai_plan(
            store, ai_sites, model=os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
            web_limit=SMART_SCAN_CANDIDATE_AI_WEB_CALLS,
        ) if ai_sites else {"selected": 0, "due": 0, "reused": 0}
        detail_limit = _smart_scan_dataforseo_limits(seo_sites)[1] if seo_sites else 0
        seo = seo_plan(store, seo_sites, detail_limit) if seo_sites else {
            "cohort": [], "bulk_due": 0, "bulk_reused": 0, "detail_due": 0,
        }
    source_enabled = due_hydration and (bool(ai_sites) if source_ai is None else source_ai)
    ai_cost = _smart_scan_ai_estimate(ai["due"], due_hydration_enabled=source_enabled)
    review_cost = _smart_scan_ai_estimate(ai["due"], due_hydration_enabled=False)
    seo_cost = (
        ((seo["bulk_due"] + DATAFORSEO_BULK_BATCH_LIMIT - 1) // DATAFORSEO_BULK_BATCH_LIMIT)
        * SMART_SCAN_DATAFORSEO_BULK_TASK_ESTIMATE_USD
        + seo["bulk_due"] * SMART_SCAN_DATAFORSEO_BULK_ITEM_ESTIMATE_USD
        + seo["detail_due"] * SMART_SCAN_DATAFORSEO_DETAIL_TASK_ESTIMATE_USD
    )
    ads = _smart_scan_ad_plan(ads_sites, ads_max_usd) if ads_sites else {}
    return {
        "ads": {key: value for key, value in ads.items()
                if key not in {"request", "candidate_ids"}},
        "people": (_smart_scan_people_estimate(people_sites) if people_sites else
                   {"selected": 0, "due": 0, "reused": 0, "estimate_usd": 0.0}),
        "reuse_days": REHYDRATION_DAYS,
        "has_unpriced_provider_usage": bool(
            due_hydration and microlink and os.getenv(MICROLINK_SECRET)
        ),
        "ai": {"selected": ai["selected"], "due": ai["due"], "reused": ai["reused"],
               "estimate_usd": float(ai_cost),
               "review_estimate_usd": float(review_cost),
               "source_estimate_usd": float(ai_cost - review_cost)},
        "seo": {"selected": len(seo["cohort"]), "bulk_due": seo["bulk_due"],
                "reused": seo["bulk_reused"], "detail_due": seo["detail_due"],
                "estimate_usd": float(seo_cost.quantize(Decimal("0.01")))},
    }


def _smart_scan_profile() -> dict[str, object]:
    dataforseo_ready = _dataforseo_configured()
    treg_ready = resolve_treg_credentials() is not None
    target_limit, detail_limit, request_cap = _smart_scan_dataforseo_limits(
        SMART_SCAN_DATAFORSEO_DEFAULT_TARGET_LIMIT
    )
    return {
        "people_limit_control_available": True,
        "people_company_default": 100,
        "paid_reuse_estimate_available": True,
        "source_ai_control_available": True,
        "openai_configured": bool(os.getenv(OPENAI_SECRET)),
        "microlink_control_available": True,
        "microlink_hydration_available": True,
        "microlink_configured": bool(os.getenv(MICROLINK_SECRET)),
        "source_ai_estimate_usd": float(_smart_scan_ai_estimate(0)),
        "provider_spend_usd": 0,
        "paid_ads_included": True,
        "paid_ads_configured": bool(os.getenv(APIFY_SECRET)),
        "paid_ads_company_default": 100, "paid_ads_company_max": 500,
        "paid_ads_default_max_usd": 5.0,
        "dataforseo_included": False,
        "dataforseo_configured": dataforseo_ready,
        "dataforseo_target_limit": target_limit,
        "dataforseo_target_default": target_limit,
        "dataforseo_target_max": SMART_SCAN_DATAFORSEO_MAX_TARGET_LIMIT,
        "dataforseo_detail_limit": detail_limit,
        "dataforseo_detail_divisor": SMART_SCAN_DATAFORSEO_DETAIL_DIVISOR,
        "dataforseo_bulk_batch_limit": DATAFORSEO_BULK_BATCH_LIMIT,
        "dataforseo_request_cap": request_cap,
        "dataforseo_estimate_usd": float(
            _smart_scan_dataforseo_estimate(target_limit)
        ),
        "dataforseo_bulk_task_estimate_usd": float(
            SMART_SCAN_DATAFORSEO_BULK_TASK_ESTIMATE_USD
        ),
        "dataforseo_bulk_item_estimate_usd": float(
            SMART_SCAN_DATAFORSEO_BULK_ITEM_ESTIMATE_USD
        ),
        "dataforseo_detail_task_estimate_usd": float(
            SMART_SCAN_DATAFORSEO_DETAIL_TASK_ESTIMATE_USD
        ),
        "dataforseo_market": "United States · English",
        "dataforseo_refresh_days": DATAFORSEO_REFRESH_DAYS,
        "people_hydration_included": True,
        "people_hydration_configured": treg_ready,
        "people_hydration_cohort_limit": TREG_PEOPLE_MAX_COHORT_SIZE,
        "people_hydration_refresh_days": TREG_PEOPLE_REFRESH_DAYS,
        "people_hydration_minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        "people_hydration_people_limit": DEFAULT_PEOPLE_LIMIT,
        "people_hydration_email_limit": 0,
        "people_hydration_email_max_cost_usd": float(DEFAULT_EMAIL_MAX_COST_USD),
        "hydration_batch_size": SMART_SCAN_HYDRATION_BATCH_SIZE,
        "scanner_batch_size": SMART_SCAN_SCANNER_BATCH_SIZE,
        "company_fresh_hours": 24,
        "source_fresh_hours": SOURCE_METHOD_FRESH_HOURS,
        "max_frontier_scans_per_batch": SMART_SCAN_MAX_FRONTIER_SCANS,
        "frontier_scanner_count": len(FRONTIER_SCANNER_IDS),
        "source_ai_tool_call_cap": SMART_SCAN_SOURCE_AI_TOOL_CALLS,
        "candidate_ai_call_cap": SMART_SCAN_CANDIDATE_AI_CALLS,
        "candidate_ai_call_max": SMART_SCAN_MAX_CANDIDATE_AI_CALLS,
        "candidate_ai_web_call_cap": SMART_SCAN_CANDIDATE_AI_WEB_CALLS,
        "ai_estimate_model": os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
        "source_ai_estimate_per_tool_call_usd": float(
            SMART_SCAN_SOURCE_AI_ESTIMATE_PER_TOOL_CALL_USD
        ),
        "candidate_ai_estimate_per_site_usd": float(
            SMART_SCAN_CANDIDATE_AI_ESTIMATE_PER_SITE_USD
        ),
        "candidate_ai_web_estimate_per_site_usd": float(
            SMART_SCAN_CANDIDATE_AI_WEB_ESTIMATE_PER_SITE_USD
        ),
        "default_ai_estimate_usd": float(
            _smart_scan_ai_estimate(SMART_SCAN_CANDIDATE_AI_CALLS)
        ),
        "server_log_limit": SMART_SCAN_LOG_LIMIT,
        "browser_log_limit": SMART_SCAN_BROWSER_LOG_LIMIT,
    }


_smart_scan_inventory_cancel: ContextVar[Any] = ContextVar(
    "smart_scan_inventory_cancel", default=None
)
_smart_scan_preparation_progress: ContextVar[Callable[[str], None] | None] = ContextVar(
    "smart_scan_preparation_progress", default=None
)


def _preparation_step(message: str) -> None:
    notify = _smart_scan_preparation_progress.get()
    if notify is not None:
        notify(message)


def _prepare_smart_scan_inventory(
    policy: float | None, notify: Callable[[str], None]
) -> dict[str, object]:
    token = _smart_scan_preparation_progress.set(notify)
    try:
        return _smart_scan_inventory_for_policy(policy)
    finally:
        _smart_scan_preparation_progress.reset(token)


def _smart_scan_inventory(
    max_company_failure_rate: float | None = None,
) -> dict[str, object]:
    _preparation_step("Checking available companies…")
    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        cancel = _smart_scan_inventory_cancel.get()
        if cancel is not None:
            store.connection.execute("PRAGMA busy_timeout=1000")
            store.connection.set_progress_handler(lambda: cancel.is_set(), 1000)
        # All counts describe one complete snapshot, never mixed page generations.
        inventory = CENSUS.read(store, max_company_failure_rate, cancel=cancel)
        _preparation_step("Checking enabled public web sources")
        sources = store.enabled_sources()
        _preparation_step("Checking accepted source coverage and scan history")
        accepted = store.list_accepted_discovery_sources()
        _preparation_step("Checking source discovery freshness")
        scout_due = store.source_scout_due()
        _preparation_step("Checking saved decision-maker coverage")
        people_inventory = store.treg_people_hydration_status(
            limit=TREG_PEOPLE_MAX_COHORT_SIZE, coverage_only=True,
        )
    people_estimate = estimate_people_hydration(
        company_count=int(people_inventory["due_companies"]),
        people_limit=DEFAULT_PEOPLE_LIMIT,
        email_limit=0,
        email_max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
    )
    _preparation_step("Validating source eligibility and provider cost limits")
    due_sources = []
    blocked_sources = []
    for row in accepted:
        suggestion = _source_suggestion(row)
        blockers = _source_activation_blockers(suggestion)
        policy = _source_scan_policy(row)
        if blockers:
            blocked_sources.append(int(row["id"]))
        elif policy["scan_eligible"]:
            due_sources.append(int(row["id"]))
    return {
        **inventory,
        "enabled_web_sources": len(sources),
        "accepted_sources": len(accepted),
        "accepted_sources_due": len(due_sources),
        "accepted_sources_blocked": len(blocked_sources),
        "source_scout_due": bool(scout_due),
        "openai_configured": bool(os.getenv(OPENAI_SECRET)),
        "apify_configured": bool(os.getenv(APIFY_SECRET)),
        "dataforseo_configured": _dataforseo_configured(),
        "people_hydration_configured": resolve_treg_credentials() is not None,
        "people_hydration_due_companies": int(people_inventory["due_companies"]),
        "people_hydration_fresh_companies": int(people_inventory["fresh_companies"]),
        "people_hydration_thin_companies": int(people_inventory["thin_companies"]),
        "people_hydration_minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        "people_hydration_estimate_usd": float(people_estimate["maximum_usd"]),
    }


def _smart_scan_inventory_for_policy(
    max_company_failure_rate: float | None,
) -> dict[str, object]:
    if max_company_failure_rate is None:
        return _smart_scan_inventory()
    return _smart_scan_inventory(max_company_failure_rate)


class SmartScanManager:
    """Compose existing scan paths into one exhaustive, cost-safe long-running job."""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self._persistence_owner = uuid.uuid4().hex
        self._persistence_sequence = 0
        self._error_resolution = ("", 0, frozenset())
        self._error_resolution_generation = 0
        self._stop_cleanup = None
        self.preparation: dict[str, Any] | None = None
        self.events: deque[dict[str, object]] = deque(maxlen=SMART_SCAN_LOG_LIMIT)
        self.actions: deque[dict[str, object]] = deque(maxlen=SMART_SCAN_ACTION_LOG_LIMIT)
        self.event_sequence = 0
        self.action_sequence = 0
        self.reported_child_run_ids: set[int] = set()
        self.control_event = asyncio.Event()
        self.control_event.set()
        self.shutting_down_for_restart = False
        self.last_runtime_tick = datetime.now(UTC)
        self.state = self._ready_state()

    @staticmethod
    def _ready_state() -> dict[str, object]:
        return {
            "status": "idle",
            "phase": "idle",
            "message": "Ready to scan",
            "phase_index": 0,
            "phase_count": len(SMART_SCAN_PHASES),
            "counters": {},
            "control_status": "idle",
            "work": {"completed": 0, "total": 0, "current": ""},
            "progress_percent": 0.0,
            "error_total": 0,
        }

    running = ScanManager.running

    @property
    def cleaning_up(self) -> bool:
        return bool(
            (self._stop_cleanup is not None and not self._stop_cleanup.done())
            or (self.running and self.state.get("status") in {
                "stopped", "complete", "partial", "failed",
            })
        )

    @property
    def active(self) -> bool:
        return (self.preparation is not None or self.running
                or self.cleaning_up
                or str(self.state.get("status") or "") in {
            "starting",
            "running",
            "paused",
            "reconnecting",
            "restarting",
        })

    def _event(
        self,
        message: str,
        *,
        level: str = "info",
        detail: str = "",
        replace_key: str | None = None,
        persist: bool = True,
    ) -> dict[str, object]:
        safe_message = " ".join(str(message).split())[:240]
        safe_detail = " ".join(str(detail).split())[:500]
        timestamp = datetime.now(UTC).isoformat()
        if replace_key:
            items = list(self.events)
            for index in range(len(items) - 1, -1, -1):
                if items[index].get("replace_key") == replace_key:
                    items[index] = {
                        **items[index],
                        "timestamp": timestamp,
                        "level": level,
                        "phase": self.state.get("phase", "idle"),
                        "message": safe_message,
                        "detail": safe_detail,
                    }
                    if replace_key == "current-child-action":
                        items.append(items.pop(index))
                        index = len(items) - 1
                    self.events = deque(items, maxlen=SMART_SCAN_LOG_LIMIT)
                    if persist:
                        self._persist_event(items[index])
                    return items[index]
        self.event_sequence += 1
        event = {
            "id": self.event_sequence,
            "timestamp": timestamp,
            "level": level,
            "phase": self.state.get("phase", "idle"),
            "message": safe_message,
            "detail": safe_detail,
            "replace_key": replace_key,
        }
        self.events.append(event)
        if persist:
            self._persist_event(event)
        return event

    def _persist_event(self, event: dict[str, object]) -> None:
        if self.state.get("id"):
            self._queue_persistence("event", (event, SMART_SCAN_LOG_LIMIT),
                                    key=("event", self.state["id"], event["id"]))

    def _action(
        self,
        message: str,
        *,
        detail: str = "",
        level: str = "item",
    ) -> None:
        self.action_sequence += 1
        action = {
            "id": self.action_sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": self.state.get("phase", "idle"),
            "message": " ".join(str(message).split())[:240],
            "detail": " ".join(str(detail).split())[:500],
            "level": level if level in {"item", "error", "skip", "cached"} else "item",
        }
        self.actions.append(action)
        self._event(
            str(action["message"]),
            detail=str(action["detail"]),
            level="item",
            replace_key="current-child-action",
            persist=False,
        )

    def _queue_persistence(self, kind, value, *, key=None):
        from .domain_policy import policy_path
        from .scan_persistence import WRITER, write_packet

        snapshot = dict(self.state)
        path = database_path()
        if kind in {"state", "error_event"}:
            self._persistence_sequence += 1
        sequence = self._persistence_sequence
        future = WRITER.submit(self._persistence_owner, key, write_packet,
                               (path, policy_path(), kind, snapshot, value))

        def apply(result):
            if (result and sequence == self._persistence_sequence
                    and result.get("id") == self.state.get("id")):
                self.state.update(result)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            apply(future.result())
        else:
            def completed(saved):
                try:
                    result = saved.result()
                except Exception:
                    return  # The worker logs the failure; durable barriers propagate it.
                if not loop.is_closed():
                    loop.call_soon_threadsafe(apply, result)
            future.add_done_callback(completed)
        return future

    async def _flush_persistence(self):
        from .scan_persistence import WRITER

        task = asyncio.wrap_future(WRITER.barrier(self._persistence_owner))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _persist_state(self) -> None:
        if not self.state.get("id"):
            return
        self.state["updated_at"] = datetime.now(UTC).isoformat()
        self._queue_persistence("state", None, key=("state", self.state["id"]))

    def _refresh_discovery_inventory(self) -> None:
        """Publish exact crawl eligibility after discovery, never on status polls."""
        if not self.state.get("due_hydration_enabled", True):
            return
        previous = dict(self.state.get("inventory") or {})
        inventory = self._read_discovery_inventory(
            database_path(), int(previous.get("maximum_candidate_id") or 0),
            self.state.get("max_company_failure_rate"),
        )
        if inventory is not None:
            self._publish_discovery_inventory(inventory)

    @staticmethod
    def _read_discovery_inventory(database, previous_boundary, max_company_failure_rate):
        # Only scalar planning results leave this read-only, committed snapshot. No thread
        # may mutate the parent state or publish a result after cancellation.
        with LeadStore(database, initialize=False, read_only=True) as store:
            store.connection.execute("BEGIN")
            boundary = int(store.connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM candidates"
            ).fetchone()[0])
            if boundary == previous_boundary:
                return None
            return store.smart_scan_inventory(
                max_candidate_id=boundary,
                max_company_failure_rate=max_company_failure_rate,
            )

    def _publish_discovery_inventory(self, inventory):
        self.state["inventory"] = {**dict(self.state.get("inventory") or {}), **inventory}
        self.state["inventory_as_of"] = datetime.now(UTC).isoformat()
        self._persist_state()

    async def _refresh_discovery_inventory_async(self) -> None:
        from .async_work import run_thread_owned

        if not self.state.get("due_hydration_enabled", True):
            return
        scan_id = self.state.get("id")
        previous_boundary = int(dict(self.state.get("inventory") or {}).get(
            "maximum_candidate_id") or 0)
        inventory = await run_thread_owned(
            self._read_discovery_inventory, database_path(), previous_boundary,
            self.state.get("max_company_failure_rate"),
        )
        if inventory is not None and self.state.get("id") == scan_id:
            self._publish_discovery_inventory(inventory)

    def _set_work(
        self,
        *,
        completed: int | None = None,
        total: int | None = None,
        current: str | None = None,
    ) -> None:
        work = dict(self.state.get("work") or {})
        if completed is not None:
            work["completed"] = max(0, int(completed))
        if total is not None:
            work["total"] = max(0, int(total))
        if current is not None:
            work["current"] = current
        completed_units = int(work.get("completed") or 0)
        total_units = int(work.get("total") or 0)
        if total_units:
            completed_units = min(completed_units, total_units)
            work["completed"] = completed_units
            ceiling = 99.0 if str(self.state.get("status")) in {
                "starting", "running", "paused", "reconnecting", "restarting",
            } else 100.0
            percent = min(ceiling, round(completed_units / total_units * 100, 2))
            phase_index = int(self.state.get("phase_index") or 0)
            phase_count = int(self.state.get("phase_count") or 0)
            if ceiling == 99.0 and phase_index and phase_count:
                # Work is discovered as we go; completed phases are the only complete
                # denominator. Never present a nearly finished page as a nearly finished scan.
                percent = round(max(0, phase_index - 1) / phase_count * 100, 2)
                self.state["progress_basis"] = "completed_phases"
        else:
            percent = 0.0
        self.state["work"] = work
        self.state["progress_percent"] = percent

    def _advance_work(self, amount: int = 1, *, current: str | None = None) -> None:
        work = dict(self.state.get("work") or {})
        self._set_work(
            completed=int(work.get("completed") or 0) + max(0, int(amount)),
            current=current,
        )
        self._persist_state()

    async def _checkpoint(self) -> None:
        await self._flush_persistence()
        while str(self.state.get("control_status") or "running") == "paused":
            self.state.update(
                {
                    "status": "paused",
                    "message": "Smart Scan paused; completed work is preserved",
                }
            )
            self._persist_state()
            await self._flush_persistence()
            self.control_event.clear()
            await self.control_event.wait()
        if str(self.state.get("control_status") or "running") == "stopped":
            raise asyncio.CancelledError
        if (
            not self.state.get("reconnect_pending")
            and self._runtime_gap_seconds() >= SMART_SCAN_SLEEP_GAP_SECONDS
        ):
            if await self._internet_available():
                self._event(
                    "Laptop wake detected",
                    detail="Internet is available; continuing Smart Scan",
                )
            else:
                await self._wait_for_internet(
                    reason="Laptop resumed before the network was ready"
                )

    def _runtime_gap_seconds(self) -> float:
        now = datetime.now(UTC)
        gap = max(0.0, (now - self.last_runtime_tick).total_seconds())
        self.last_runtime_tick = now
        return gap

    async def _internet_available(self) -> bool:
        async def reachable(host: str, port: int) -> bool:
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=2,
                )
                writer.close()
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
                return True
            except (OSError, TimeoutError):
                return False

        results = await asyncio.gather(
            *(reachable(host, port) for host, port in SMART_SCAN_CONNECTIVITY_ENDPOINTS)
        )
        return any(results)

    async def _wait_for_internet(self, *, reason: str) -> None:
        if await self._internet_available():
            was_reconnecting = bool(self.state.get("reconnect_pending"))
            self.state["reconnect_pending"] = False
            self.state["reconnect_started_at"] = None
            if was_reconnecting:
                self.state.update(
                    {
                        "status": "running",
                        "message": "Internet restored; resuming Smart Scan",
                    }
                )
                self._event(
                    "Internet connection restored",
                    level="success",
                    detail="Retrying the interrupted work item",
                )
                self._persist_state()
            return
        first_wait = not bool(self.state.get("reconnect_pending"))
        self.state.update(
            {
                "status": "reconnecting",
                "message": "Internet unavailable; Smart Scan is waiting to reconnect",
                "reconnect_pending": True,
                "reconnect_started_at": self.state.get("reconnect_started_at")
                or datetime.now(UTC).isoformat(),
            }
        )
        if first_wait:
            self._event(
                "Internet connection lost",
                level="warn",
                detail=f"{reason}; completed work is preserved and this item will retry",
            )
        self._persist_state()
        while True:
            await self._checkpoint()
            await asyncio.sleep(SMART_SCAN_RECONNECT_RETRY_SECONDS)
            if await self._internet_available():
                break
        self.state.update(
            {
                "status": "running",
                "message": "Internet restored; resuming Smart Scan",
                "reconnect_pending": False,
                "reconnect_started_at": None,
            }
        )
        self.last_runtime_tick = datetime.now(UTC)
        self._event(
            "Internet connection restored",
            level="success",
            detail="Retrying the interrupted work item",
        )
        self._persist_state()

    def _set_phase(self, phase_id: str, message: str) -> None:
        phase_index = next(
            (
                index
                for index, phase in enumerate(SMART_SCAN_PHASES, start=1)
                if phase["id"] == phase_id
            ),
            0,
        )
        timestamp = datetime.now(UTC).isoformat()
        if self.state.get("phase") != phase_id or not self.state.get("phase_started_at"):
            self.state["phase_started_at"] = timestamp
        self.state.update(
            {
                "status": "running",
                "phase": phase_id,
                "phase_index": phase_index,
                "phase_count": len(SMART_SCAN_PHASES),
                "message": message,
                "updated_at": timestamp,
            }
        )
        self._set_work(current=message)
        self._event(message)
        self._persist_state()

    def status_payload(
        self, *, refresh_inventory: bool = False, fast: bool = False
    ) -> dict[str, object]:
        payload = {
            **self.state,
            "running": self.running,
            "cleaning_up": self.cleaning_up,
            "external_source_runs": getattr(
                discovery_method_manager, "active_payloads", lambda: []
            )(),
            "profile": _smart_scan_profile(),
            "phases": list(SMART_SCAN_PHASES),
            "events": list(self.events),
            "actions": list(self.actions),
        }
        for internal_key in ("_dataforseo_cohort", "_dataforseo_detail_cohort",
                             "_candidate_ai_plan", "_people_due_cohort", "_dataforseo_due_plan",
                             "_microlink_cohort", "scan_audit"):
            payload.pop(internal_key, None)
        if self.preparation is not None:
            preparation = self.preparation
            return {
                **payload,
                **preparation["options"],
                "id": preparation["id"],
                "finished_at": None,
                "resumed_after_restart_at": None,
                "status": "starting",
                "phase": "planning",
                "preparing": True,
                "message": preparation["message"],
                "started_at": preparation["started_at"],
                "updated_at": preparation["updated_at"],
                "phase_index": 1,
                "progress_percent": 0.0,
                "control_status": "preparing",
                "inventory_pending": True,
                "events": list(preparation["events"]),
                "actions": [],
                "counters": {},
                "error_total": 0,
                "work": {"completed": 0, "total": 0, "current": preparation["message"]},
            }
        if payload.get("id"):
            from .error_recovery import activity_signature

            cached_run, resolved_count, signatures = self._error_resolution
            if cached_run != str(payload["id"]):
                resolved_count, signatures = 0, frozenset()
            # Pure owner-memory presentation: includes pending async error writes.
            payload["error_total"] = max(0, int(payload.get("error_total") or 0) - resolved_count)
            payload["error_resolution_revision"] = resolved_count
            for key in ("events", "actions"):
                payload[key] = [row for row in payload[key]
                                if activity_signature(row) not in signatures]
        if fast:
            # Status polling must never traverse the company inventory. A saved
            # idle inventory is historical, not a current launch estimate.
            payload["inventory_pending"] = not self.running or not isinstance(
                payload.get("inventory"), dict
            )
            payload.setdefault("inventory", None)
            payload.setdefault("inventory_as_of", None)
            return payload
        if refresh_inventory or "inventory" not in payload:
            payload["inventory"] = _smart_scan_inventory_for_policy(
                self.state.get("max_company_failure_rate")
            )
            payload["inventory_as_of"] = datetime.now(UTC).isoformat()
        payload["inventory_pending"] = False
        return payload

    async def recover(self) -> bool:
        if self.running:
            return False
        with LeadStore(database_path()) as store:
            recovered = store.latest_smart_scan_run()
            if recovered and recovered.get("cleared_at"):
                return False
            events = (
                store.smart_scan_events(str(recovered.get("id")), limit=SMART_SCAN_LOG_LIMIT)
                if recovered and recovered.get("id")
                else []
            )
        if not recovered:
            return False
        self.state = recovered
        if "new_companies_added" not in self.state:
            self._persist_state()
        self.events = deque(events, maxlen=SMART_SCAN_LOG_LIMIT)
        self.actions.clear()
        self.action_sequence = 0
        self.reported_child_run_ids.clear()
        self.event_sequence = max((int(item.get("id") or 0) for item in events), default=0)
        # Restore scrubbed presentation once off-loop, including paused/terminal
        # runs which return below. Subsequent status polls remain memory-only.
        await self.refresh_error_presentation(limit=1)
        if str(recovered.get("control_status")) != "running" or str(
            recovered.get("status")
        ) in {"complete", "partial", "failed", "stopped"}:
            return False
        self.shutting_down_for_restart = False
        self.last_runtime_tick = datetime.now(UTC)
        inventory = _smart_scan_inventory_for_policy(
            recovered.get("max_company_failure_rate")
        )
        completed = int(dict(recovered.get("work") or {}).get("completed") or 0)
        recovered_counters = dict(recovered.get("counters") or {})
        recovered_seo_limit = int(
            recovered.get("seo_site_limit")
            or recovered_counters.get("dataforseo_target_cap")
            or 2_500
        )
        recovered_seo_limit, recovered_detail_limit, _ = _smart_scan_dataforseo_limits(
            recovered_seo_limit
        )
        due_hydration_enabled = bool(recovered.get("due_hydration_enabled", True))
        remaining_units = (
            (
                int(inventory.get("due_candidates") or 0)
                + int(inventory.get("accepted_sources_due") or 0)
                + 5
                + (len(RSS_SOURCES) - len(recovered.get("rss_sources_done") or [])
                   if not recovered.get("source_phase_complete") and recovered.get("phase") not in {
                       "base_hydration", "seo_correction", "paid_ads",
                       "people_hydration", "ai_shortlist"
                   } else 0)
                if due_hydration_enabled else 1
            )
            + (
                recovered_seo_limit + recovered_detail_limit
                if recovered.get("dataforseo_enabled")
                and not recovered.get("dataforseo_complete")
                else 0
            )
            + (
                (int(_smart_scan_people_estimate(int(recovered["people_company_limit"]))["due"])
                 if recovered.get("people_company_limit") else
                 int(inventory.get("people_hydration_due_companies") or 0))
                if recovered.get("people_hydration_enabled")
                and not recovered.get("people_hydration_complete")
                else 0
            )
            + (int(recovered.get("paid_ads_company_limit") or 0)
               if recovered.get("paid_ads_enabled")
               and not recovered.get("paid_ads_complete") else 0)
            + (int(recovered.get("ai_site_limit") or 0) if recovered.get("ai_enabled") else 0)
            + (1 if MicrolinkHydrationOptions.model_validate(
                recovered.get("microlink_hydration") or {}).enabled
               and not recovered.get("microlink_hydration_complete") else 0)
        )
        self.state.update(
            {
                "status": "starting",
                "message": "Resuming Smart Scan after app restart",
                "resumed_after_restart_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "inventory": inventory,
                "due_hydration_enabled": due_hydration_enabled,
                "seo_enabled": bool(recovered.get("dataforseo_enabled")),
                "seo_site_limit": (
                    recovered_seo_limit if recovered.get("dataforseo_enabled") else 0
                ),
                "dataforseo_detail_limit": (
                    recovered_detail_limit if recovered.get("dataforseo_enabled") else 0
                ),
                "people_hydration_enabled": bool(recovered.get("people_hydration_enabled", False)),
            }
        )
        self._set_work(completed=completed, total=completed + remaining_units)
        self._event("Smart Scan resumed after app restart")
        self._persist_state()
        await self._flush_persistence()
        self.task = asyncio.create_task(self._run())
        return True

    async def start(
        self,
        request: SmartScanStartRequest | None = None,
    ) -> dict[str, object]:
        request = request or SmartScanStartRequest()
        if self.active:
            raise HTTPException(status_code=409, detail="A Smart Scan is already running")
        conflicts = []
        if scan_manager.running:
            conflicts.append("company scan")
        if discovery_method_manager.running:
            conflicts.append("public-source scan")
        if source_scout_manager.running:
            conflicts.append("source scout")
        if ad_discovery_manager.running:
            conflicts.append("paid-ad scan")
        if people_hydration_manager.running:
            conflicts.append("people hydration")
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=f"Wait for the active {conflicts[0]} before starting Smart Scan",
            )
        if request.microlink_hydration.enabled and not request.due_hydration_enabled:
            try:
                require_connected_owner(database_path())
            except DesktopRankingUnavailable as exc:
                raise DesktopRankingUnavailable(
                    "Connect a compatible Beings Growth desktop ranking worker before Microlink."
                ) from exc
        timestamp = datetime.now(UTC).isoformat()
        preparation: dict[str, Any] = {
            "started_at": timestamp, "updated_at": timestamp,
            "message": "Checking scan preparation", "events": [],
            "id": uuid.uuid4().hex[:12],
            "options": {
                "due_hydration_enabled": request.due_hydration_enabled,
                "search_terms": request.search_terms,
                "paid_ads_enabled": request.paid_ads_enabled,
                "paid_ads_company_limit": request.paid_ads_company_limit,
                "paid_ads_max_usd": float(request.paid_ads_max_usd),
                "people_company_limit": request.people_company_limit,
                "people_hydration_enabled": request.people_hydration_enabled,
                "ai_enabled": request.ai_enabled,
                "source_ai_enabled": request.source_ai_enabled,
                "microlink_enabled": request.microlink_enabled,
                "microlink_hydration": request.microlink_hydration.model_dump(),
                "ai_site_limit": request.ai_site_limit,
                "dataforseo_enabled": request.seo_enabled,
                "seo_site_limit": request.seo_site_limit,
                "max_company_failure_rate": request.max_company_failure_rate,
            },
        }
        self.preparation = preparation
        loop = asyncio.get_running_loop()

        def report(message: str) -> None:
            if self.preparation is not preparation:
                return
            timestamp = datetime.now(UTC).isoformat()
            preparation.update(message=message, updated_at=timestamp)
            preparation["events"].append({
                "id": len(preparation["events"]) + 1,
                "timestamp": timestamp, "level": "info", "phase": "planning",
                "message": message, "detail": "", "replace_key": None,
            })

        preparation_started = monotonic()
        report("Checking scan preparation")
        try:
            # The display-only read can use a different failure policy. Release its
            # SQLite snapshot before building the plan for the requested options.
            await smart_scan_inventory_check.shutdown()
            worker = asyncio.create_task(asyncio.to_thread(
                _prepare_smart_scan_inventory, request.max_company_failure_rate,
                lambda message: loop.call_soon_threadsafe(report, message),
            ))
            try:
                inventory = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # to_thread cannot stop an executing SQLite read. Keep the launch
                # reservation until it settles, without starting provider work.
                with suppress(Exception):
                    await worker
                raise
            if (request.paid_ads_enabled or request.ai_enabled or request.source_ai_enabled
                    or request.seo_enabled
                    or request.microlink_enabled
                    or (request.people_hydration_enabled
                        and inventory.get("people_hydration_configured"))):
                report("Checking 90-day paid-scan reuse and proposed costs")
                try:
                    preparation["paid_estimate"] = await asyncio.to_thread(
                        _smart_scan_paid_estimate,
                        request.ai_site_limit if request.ai_enabled else 0,
                        request.seo_site_limit if request.seo_enabled else 0,
                        request.due_hydration_enabled,
                        request.source_ai_enabled,
                        bool(request.microlink_enabled),
                        **({"ads_sites": request.paid_ads_company_limit,
                            "ads_max_usd": request.paid_ads_max_usd}
                           if request.paid_ads_enabled else {}),
                        **({"people_sites": request.people_company_limit}
                           if request.people_hydration_enabled
                           and inventory.get("people_hydration_configured") else {}),
                    )
                except Exception:
                    # Reuse estimates are advisory; execution retains its paid caps.
                    report("Cost estimate unavailable; using configured scan limits")

            preparation["elapsed_seconds"] = monotonic() - preparation_started
            return await self._start_prepared(request, inventory, preparation)
        finally:
            if self.preparation is preparation:
                self.preparation = None

    async def _start_prepared(
        self, request: SmartScanStartRequest, inventory: dict[str, object],
        preparation: dict[str, Any],
    ) -> dict[str, object]:
        # Another collector may have started while the read-only plan was built.
        for manager, label in (
            (scan_manager, "company scan"),
            (discovery_method_manager, "public-source scan"),
            (source_scout_manager, "source scout"),
            (ad_discovery_manager, "paid-ad scan"),
            (people_hydration_manager, "people hydration"),
        ):
            if manager.running:
                raise HTTPException(
                    status_code=409,
                    detail=f"Wait for the active {label} before starting Smart Scan",
                )
        if request.paid_ads_enabled and not os.getenv(APIFY_SECRET):
            raise HTTPException(422, "Configure Apify in Settings before enabling Paid ads")
        if request.microlink_hydration.enabled and not os.getenv(MICROLINK_SECRET):
            raise HTTPException(422, "Configure Microlink in Settings before enabling hydration")
        if request.people_hydration_enabled and not inventory.get("people_hydration_configured"):
            raise HTTPException(
                status_code=422,
                detail="Configure Treg in Settings before enabling People Hydration",
            )
        if request.seo_enabled and not bool(inventory.get("dataforseo_configured")):
            raise HTTPException(
                status_code=422,
                detail="Configure DataForSEO credentials in Settings before enabling SEO",
            )
        self.events.clear()
        self.actions.clear()
        self.events.extend(preparation["events"])
        self.event_sequence = len(self.events)
        self.action_sequence = 0
        self.reported_child_run_ids.clear()
        self.shutting_down_for_restart = False
        self.last_runtime_tick = datetime.now(UTC)
        self.control_event.set()
        started_at = str(preparation["started_at"])
        source_units = (
            int(inventory.get("accepted_sources_due") or 0) + 2 + len(RSS_SOURCES)
            if request.due_hydration_enabled else 0
        )
        ai_units = request.ai_site_limit if request.ai_enabled else 0
        dataforseo_enabled = request.seo_enabled
        seo_site_limit, dataforseo_detail_limit, dataforseo_request_cap = (
            _smart_scan_dataforseo_limits(request.seo_site_limit)
        )
        dataforseo_units = (
            seo_site_limit + dataforseo_detail_limit if dataforseo_enabled else 0
        )
        people_hydration_enabled = request.people_hydration_enabled and bool(
            inventory.get("people_hydration_configured")
        )
        people_hydration_units = (
            int(preparation.get("paid_estimate", {}).get("people", {}).get("due",
                min(request.people_company_limit,
                    int(inventory.get("people_hydration_due_companies") or 0))))
            if people_hydration_enabled
            else 0
        )
        total_work_units = (
            (int(inventory.get("due_candidates") or 0) if request.due_hydration_enabled else 0)
            + source_units
            + ai_units
            + dataforseo_units
            + people_hydration_units
            + (request.paid_ads_company_limit if request.paid_ads_enabled else 0)
            + (2 if request.due_hydration_enabled else 1)
            + int(request.microlink_hydration.enabled)
        )
        self.state = {
            "id": preparation["id"],
            "scan_audit": {"operations": {"planning": {
                "elapsed_seconds": preparation.get("elapsed_seconds", 0.0),
                "outcomes": {"complete": 1},
                "results": {},
            }}},
            "status": "starting",
            "phase": "planning",
            "message": "Mapping source and hydration gaps",
            "phase_index": 1,
            "phase_count": len(SMART_SCAN_PHASES),
            "started_at": started_at,
            "updated_at": started_at,
            "inventory": inventory,
            "new_company_start_id": int(inventory.get("maximum_candidate_id") or 0),
            "new_companies_added": 0,
            "max_company_failure_rate": request.max_company_failure_rate,
            "due_hydration_enabled": request.due_hydration_enabled,
            "search_terms": request.search_terms,
            "ai_enabled": request.ai_enabled,
            "source_ai_enabled": request.source_ai_enabled,
            "microlink_enabled": request.microlink_enabled,
            "microlink_hydration": request.microlink_hydration.model_dump(),
            "microlink_hydration_complete": not request.microlink_hydration.enabled,
            "microlink_hydration_request_cap": (
                request.microlink_hydration.request_limit
                if request.microlink_hydration.enabled else 0),
            "ai_site_limit": request.ai_site_limit if request.ai_enabled else 0,
            "estimated_ai_cost_usd": (
                preparation.get("paid_estimate", {}).get("ai", {}).get(
                    "estimate_usd", float(_smart_scan_ai_estimate(
                    request.ai_site_limit if request.ai_enabled else 0,
                    due_hydration_enabled=bool(request.source_ai_enabled),
                )))
                if request.ai_enabled or request.source_ai_enabled
                else 0.0
            ),
            "estimated_ai_review_cost_usd": (
                preparation.get("paid_estimate", {}).get("ai", {}).get(
                    "review_estimate_usd", float(_smart_scan_ai_estimate(
                        request.ai_site_limit, due_hydration_enabled=False,
                    )),
                ) if request.ai_enabled else 0.0
            ),
            "has_unpriced_provider_usage": bool((request.microlink_enabled
                                                or request.microlink_hydration.enabled)
                                               and os.getenv(MICROLINK_SECRET)),
            "estimated_source_ai_cost_usd": (
                preparation.get("paid_estimate", {}).get("ai", {}).get(
                    "source_estimate_usd", float(_smart_scan_ai_estimate(0)),
                ) if request.source_ai_enabled else 0.0
            ),
            "seo_enabled": dataforseo_enabled,
            "seo_site_limit": seo_site_limit if dataforseo_enabled else 0,
            "dataforseo_enabled": dataforseo_enabled,
            "dataforseo_complete": not dataforseo_enabled,
            "dataforseo_detail_limit": (
                dataforseo_detail_limit if dataforseo_enabled else 0
            ),
            "dataforseo_request_cap": (
                dataforseo_request_cap if dataforseo_enabled else 0
            ),
            "estimated_dataforseo_cost_usd": (
                preparation.get("paid_estimate", {}).get("seo", {}).get(
                    "estimate_usd", float(_smart_scan_dataforseo_estimate(seo_site_limit)))
                if dataforseo_enabled
                else 0.0
            ),
            "actual_dataforseo_cost_usd": 0.0,
            "paid_ads_enabled": request.paid_ads_enabled,
            "paid_ads_company_limit": request.paid_ads_company_limit,
            "paid_ads_max_usd": float(request.paid_ads_max_usd),
            "paid_ads_complete": not request.paid_ads_enabled,
            "paid_ads_run_id": None,
            "estimated_paid_ads_cost_usd": float(
                preparation.get("paid_estimate", {}).get("ads", {}).get("estimate_usd", 0)),
            "actual_paid_ads_cost_usd": 0.0,
            "people_company_limit": request.people_company_limit,
            "people_hydration_enabled": people_hydration_enabled,
            "people_hydration_complete": not people_hydration_enabled,
            "estimated_people_hydration_cost_usd": (
                float(preparation.get("paid_estimate", {}).get("people", {}).get(
                    "estimate_usd", inventory.get("people_hydration_estimate_usd") or 0))
                if people_hydration_enabled
                else 0.0
            ),
            "actual_people_hydration_cost_usd": 0.0,
            "people_hydration": {},
            "control_status": "running",
            "source_pipeline_version": 2,
            "work": {
                "completed": 0,
                "total": total_work_units,
                "current": "Mapping source and hydration gaps",
            },
            "progress_percent": 0.0,
            "error_total": 0,
            "reconnect_pending": False,
            "reconnect_started_at": None,
            "counters": {
                "source_runs_completed": 0,
                "source_runs_failed": 0,
                "hydration_batches_completed": 0,
                "hydration_hosts_submitted": 0,
                "hydration_network_scanned": 0,
                "hydration_reused": 0,
                "scanner_batches_completed": 0,
                "scanner_hosts_submitted": 0,
                "scanner_executions_completed": 0,
                "scanner_executions_reused": 0,
                "candidate_ai_cap": request.ai_site_limit if request.ai_enabled else 0,
                "candidate_ai_reviewed": 0,
                "candidate_ai_fresh_calls": 0,
                "dataforseo_target_cap": seo_site_limit if dataforseo_enabled else 0,
                "dataforseo_detail_cap": (
                    dataforseo_detail_limit if dataforseo_enabled else 0
                ),
                "dataforseo_request_cap": (
                    dataforseo_request_cap if dataforseo_enabled else 0
                ),
                "dataforseo_requests": 0,
                "dataforseo_bulk_returned": 0,
                "dataforseo_detail_returned": 0,
                "dataforseo_reprojected": 0,
                "dataforseo_failures": 0,
                "people_hydration_due": people_hydration_units,
                "people_hydration_completed": 0,
                "people_hydration_reused": 0,
                "people_hydration_failed": 0,
                "people_hydration_contacts": 0,
                "people_hydration_quality_protected": 0,
            },
        }
        self._persist_state()
        for event in self.events:
            self._persist_event(event)
        self._event(
            "Smart Scan plan ready",
            detail=(
                (
                    f"{inventory['due_candidates']:,} company records due · "
                    f"{inventory['accepted_sources_due']:,} accepted sources due · "
                    if request.due_hydration_enabled
                    else "Due hydration off · existing inventory only · "
                )
                + (
                    f"AI enabled for up to {request.ai_site_limit:,} final sites"
                    if request.ai_enabled
                    else "Company reviews off"
                )
                + (
                    f" · SEO enabled for the top {seo_site_limit:,} sites "
                    f"plus {dataforseo_detail_limit:,} detailed checks"
                    if dataforseo_enabled
                    else " · SEO disabled"
                )
                + (
                    f" · people hydration due for {people_hydration_units:,} ranked companies"
                    if people_hydration_enabled
                    else " · people hydration off"
                )
            ),
        )
        await self._flush_persistence()
        self.task = asyncio.create_task(self._run())
        self.preparation = None
        return self.status_payload()

    def _record_error(
        self,
        detail: str,
        *,
        event_message: str = "A stage finished with an error",
        activity: str = "",
        child_run_id: int = 0,
        child_job_id: str = "",
    ) -> None:
        safe_message = " ".join(str(detail).split())[:500]
        lower = safe_message.lower()
        gap_labels = (
            (("website check deferred by local request capacity",),
             "Website check deferred by local capacity"),
            (
                ("context_length_exceeded", "context window", "maximum context length"),
                "AI request failed: model context limit exceeded",
            ),
            (
                ("robots_blocked", "robots.txt disallows", "robots.txt redirect"),
                "Research restricted by robots policy",
            ),
            (("host temporarily deferred",), "Research deferred while the host recovers"),
            (
                (
                    "core website url is already hydrated",
                    "already-hydrated core website",
                    "surface owned by another scanner",
                    "non-document asset",
                    "targeted an asset",
                ),
                "Duplicate or non-document research skipped",
            ),
            (
                ("identity-bound, semantically valid public careers",),
                "Company-matched public careers evidence unavailable",
            ),
            (("identity_ambiguous",), "Research source identity could not be confirmed"),
            (
                (
                    "http 429",
                    "rate_limit",
                    "rate limit",
                    "ratelimiterror",
                    "request deferred locally",
                ),
                "Research rate-limited; retry deferred",
            ),
            (
                ("certificate_verify_failed", "certificate verify failed"),
                "Website certificate could not be verified",
            ),
            (("dns resolution failed",), "Website domain could not be resolved"),
            (("unrendered javascript shell",), "Rendered website content unavailable"),
            (("http 401", "http 402", "http 403", "http 406"), "Research page access restricted"),
            (("http 404",), "Research page unavailable"),
            (("http 500", "http 502", "http 503", "http 504"), "Research server unavailable"),
            (
                ("transient_error", "timeoutexception", "readtimeout", "connecttimeout"),
                "Temporary research failure",
            ),
        )
        for terms, label in gap_labels:
            if any(term in lower for term in terms):
                event_message = label
                break
        if "association file is invalid json" in lower:
            event_message = "Invalid app-association document"
        elif "http 404" in lower and any(
            marker in lower for marker in ("apple-app-site-association", "assetlinks.json")
        ):
            event_message = "Optional app-association file unavailable"
        context = activity or str(dict(self.state.get("work") or {}).get("current") or "")
        if context:
            identifiers = []
            if child_run_id:
                identifiers.append(f"run {child_run_id}")
            if child_job_id:
                identifiers.append(f"job {child_job_id}")
            suffix = f" ({', '.join(identifiers)})" if identifiers else ""
            safe_message = f"Activity: {context[:200]}{suffix} | {safe_message}"
        counters = dict(self.state.get("counters") or {})
        counters["errors"] = int(counters.get("errors") or 0) + 1
        self.state["counters"] = counters
        self.state["error_total"] = int(self.state.get("error_total") or 0) + 1
        phase_errors = dict(self.state.get("phase_errors") or {})
        phase = str(self.state.get("phase") or "unknown")
        phase_errors[phase] = int(phase_errors.get(phase) or 0) + 1
        self.state["phase_errors"] = phase_errors
        timestamp = datetime.now(UTC).isoformat()
        scan_id = str(self.state.get("id") or "")
        event = self._event(event_message, level="error", detail=safe_message, persist=False)
        if scan_id:
            self.state["updated_at"] = timestamp
            self._queue_persistence("error_event", ({
                "phase": str(self.state.get("phase") or "unknown"),
                "message": event_message, "detail": safe_message, "timestamp": timestamp,
            }, event, SMART_SCAN_LOG_LIMIT))

    async def pause(self) -> dict[str, object]:
        if not self.running:
            raise HTTPException(status_code=409, detail="No running Smart Scan to pause")
        if str(self.state.get("control_status")) == "paused":
            return self.status_payload()
        self.state.update(
            {
                "control_status": "paused",
                "status": "paused",
                "message": "Smart Scan paused; completed work is preserved",
            }
        )
        self.control_event.clear()
        self._event(
            "Smart Scan paused",
            detail="The active child was interrupted; Resume continues from durable checkpoints",
        )
        self._persist_state()
        await self._flush_persistence()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return self.status_payload()

    async def resume(self) -> dict[str, object]:
        if str(self.state.get("control_status")) != "paused":
            raise HTTPException(status_code=409, detail="Smart Scan is not paused")
        if self.task is not None and not self.task.done():
            with suppress(asyncio.CancelledError):
                await self.task
        self.state.update(
            {
                "control_status": "running",
                "status": "running",
                "message": "Smart Scan resuming",
                "finished_at": None,
            }
        )
        self.control_event.set()
        self._event("Smart Scan resumed")
        self._persist_state()
        await self._flush_persistence()
        self.task = asyncio.create_task(self._run())
        return self.status_payload()

    async def stop(self) -> dict[str, object]:
        if self.cleaning_up:
            raise HTTPException(status_code=409, detail="Smart Scan cleanup is already in progress")
        if (str(self.state.get("status")) in {"idle", "complete", "partial", "failed", "stopped"}
                and not discovery_method_manager.running):
            raise HTTPException(status_code=409, detail="No active Smart Scan to stop")
        timestamp = datetime.now(UTC).isoformat()
        self.state.update(
            {
                "control_status": "stopped",
                "status": "stopped",
                "phase": "stopped",
                "message": "Smart Scan stopped; completed work is preserved",
                "finished_at": timestamp,
                "updated_at": timestamp,
            }
        )
        self.control_event.set()
        self._event(str(self.state["message"]), level="warn")
        self._persist_state()
        persisted = asyncio.create_task(self._flush_persistence())

        scan_task = self.task
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()

        async def finish_stop():
            await discovery_method_manager.cancel_all()
            if scan_task is not None:
                with suppress(asyncio.CancelledError):
                    await scan_task
            await asyncio.shield(persisted)

        self._stop_cleanup = asyncio.create_task(finish_stop())
        return self.status_payload(fast=True)

    async def clear(self) -> dict[str, object]:
        """Return to a ready view once the terminal run has finished cleanup."""
        if self.active or discovery_method_manager.running:
            raise HTTPException(
                status_code=409,
                detail="Stop Smart Scan and wait for cleanup before clearing it",
            )
        await self._flush_persistence()
        scan_id = str(self.state.get("id") or "")
        if scan_id:
            def clear_saved():
                with LeadStore(database_path()) as store:
                    store.clear_smart_scan_presentation(scan_id)
            try:
                await asyncio.to_thread(clear_saved)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        self.state = self._ready_state()
        self.task = None
        self.events.clear()
        self.actions.clear()
        self.event_sequence = 0
        self.action_sequence = 0
        self.reported_child_run_ids.clear()
        self.control_event.set()
        self.shutting_down_for_restart = False
        self.last_runtime_tick = datetime.now(UTC)
        return self.status_payload(fast=True)

    def _error_presentation_request(self, offset, limit):
        self._error_resolution_generation += 1
        return {
            "scan_id": str(self.state.get("id") or ""),
            "generation": self._error_resolution_generation,
            "path": database_path(), "offset": offset, "limit": limit,
            "activity": [*self.events, *self.actions],
        }

    @staticmethod
    def _read_error_presentation(request):
        if not request["scan_id"]:
            return {"items": [], "total": 0, "offset": request["offset"],
                    "limit": request["limit"], "has_more": False}
        return LeadStore.read_smart_scan_errors(
            request["path"], request["scan_id"], offset=request["offset"],
            limit=request["limit"], activity=request["activity"],
        )

    def _publish_error_presentation(self, request, payload):
        signatures = payload.pop("_resolved_activity", frozenset())
        if (request["scan_id"] == str(self.state.get("id") or "")
                and request["generation"] == self._error_resolution_generation):
            self._error_resolution = (
                request["scan_id"], int(payload.get("resolved_total") or 0),
                frozenset(signatures),
            )
            payload["operational_total"] = max(
                0, int(self.state.get("error_total") or 0) - self._error_resolution[1],
            )
        return payload

    def errors_payload(self, *, offset: int = 0, limit: int = 100) -> dict[str, object]:
        """Explicit synchronous read for local callers; never called by status polls."""
        request = self._error_presentation_request(offset, limit)
        return self._publish_error_presentation(request, self._read_error_presentation(request))

    async def refresh_error_presentation(self, *, offset=0, limit=100):
        from .async_work import run_thread_owned

        # Snapshot and publication stay on the owner loop. The worker only reads
        # captured inputs; cancellation drains it and cannot publish a late result.
        request = self._error_presentation_request(offset, limit)
        payload = await run_thread_owned(self._read_error_presentation, request)
        return self._publish_error_presentation(request, payload)

    def _promote_child_result_errors(
        self,
        result: Mapping[str, object],
        *,
        activity: str,
        errors_already_recorded: bool = False,
    ) -> None:
        if str(result.get("status") or "") != "partial":
            return
        run_id = int(result.get("run_id") or 0)
        if run_id and run_id in self.reported_child_run_ids:
            return
        if run_id:
            self.reported_child_run_ids.add(run_id)
        summary: dict[str, object] = {}
        if run_id:
            try:
                with LeadStore(database_path()) as store:
                    summary = dict(store.run_metadata(run_id).get("summary") or {})
            except Exception:
                logger.exception("failed to load partial child summary run_id=%s", run_id)
        details: list[str] = []
        for key in ("free_intelligence", "common_crawl", "news_intelligence"):
            section = summary.get(key)
            if isinstance(section, Mapping):
                details.extend(str(item) for item in section.get("errors", []) if item)
        frontier = summary.get("frontier_intelligence")
        if isinstance(frontier, Mapping):
            details.extend(str(item) for item in frontier.get("errors", []) if item)
        details.extend(str(item) for item in summary.get("ai_errors", []) if item)
        unique_details = list(dict.fromkeys(" ".join(item.split())[:500] for item in details))
        if not unique_details and errors_already_recorded:
            return
        if not unique_details:
            failures = int(summary.get("failure_count") or 0)
            unique_details = [
                f"{activity} completed with {failures or 1} recoverable child failure(s)"
            ]
        for detail in unique_details:
            self._record_error(
                detail,
                event_message=f"{activity.capitalize()} completed with a recoverable gap",
                activity=activity,
                child_run_id=run_id,
            )

    async def _wait_for_scan(
        self,
        request: ScanRequest,
        sources: list[str],
        maximum: int,
        *,
        work_units: int = 0,
        activity: str = "company scan",
    ) -> dict[str, object]:
        request.microlink_enabled = bool(self.state.get("microlink_enabled", True))
        baseline = int(dict(self.state.get("work") or {}).get("completed") or 0)
        error_baseline = int(self.state.get("error_total") or 0)
        while True:
            await self._checkpoint()
            if self.state.get("reconnect_pending"):
                await self._wait_for_internet(reason="Resuming interrupted network work")
            result = await self._wait_for_scan_attempt(
                request,
                sources,
                maximum,
                work_units=work_units,
                activity=activity,
                work_baseline=baseline,
            )
            if str(result.get("status")) == "reconnect_retry":
                self._event(
                    "Retrying interrupted child work",
                    detail=activity,
                )
                continue
            if str(result.get("status")) in {
                "failed",
                "partial",
            } and not await self._internet_available():
                await self._wait_for_internet(
                    reason=f"{activity.capitalize()} ended while offline"
                )
                self._event(
                    "Retrying child work after reconnection",
                    detail=activity,
                )
                continue
            self._promote_child_result_errors(
                result,
                activity=activity,
                errors_already_recorded=(
                    int(self.state.get("error_total") or 0) > error_baseline
                ),
            )
            return result

    @measure_operation(lambda *args, activity="company scan", **kwargs: activity, child="run_id")
    async def _wait_for_scan_attempt(
        self,
        request: ScanRequest,
        sources: list[str],
        maximum: int,
        *,
        work_units: int = 0,
        activity: str = "company scan",
        work_baseline: int = 0,
    ) -> dict[str, object]:
        from .async_work import run_thread_owned

        def read_child_activity(
            job_id: str, run_id: int, snapshot_id: int, error_id: int,
        ):
            # The child can hold the SQLite writer while publishing a large company
            # batch. Keep status reads on one read-only snapshot off the parent loop.
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                store.connection.execute("BEGIN")
                progress = store.active_run_progress(job_id)
                actual_run_id = int(progress.get("run_id") or run_id) if progress else run_id
                activities = (
                    store.run_snapshot_activity(
                        actual_run_id, after_snapshot_id=snapshot_id,
                    ) if actual_run_id else []
                )
                errors = (
                    store.run_source_error_activity(
                        actual_run_id, after_error_id=error_id,
                    ) if actual_run_id else []
                )
            return progress, actual_run_id, activities, errors

        started = await scan_manager.start(request, sources, maximum)
        job_id = str(started.get("job_id") or "")
        self.state["child_scan_job_id"] = job_id or None
        job = scan_manager.jobs.get(job_id) if job_id else None
        if job is not None and job.task is not None:
            most_processed = 0
            snapshot_cursor = 0
            source_error_cursor = 0
            child_action_cursor = 0
            child_run_id = 0
            last_phase = ""
            last_research_counts: tuple[int, int] | None = None
            has_item_activity = False
            progress: dict[str, object] | None = None
            next_progress_refresh = 0.0
            stage_replace_key = f"child-stage:{activity}"
            item_replace_key = f"child-item:{activity}"
            next_connectivity_probe = (
                asyncio.get_running_loop().time() + SMART_SCAN_CONNECTIVITY_PROBE_SECONDS
            )

            def emit_activities(
                activities: list[dict[str, object]],
                child_phase: str,
            ) -> None:
                nonlocal snapshot_cursor, has_item_activity
                for item in activities:
                    snapshot_cursor = max(snapshot_cursor, int(item.get("id") or 0))
                    kind, message, detail = _smart_scan_snapshot_presentation(
                        item,
                        child_phase,
                    )
                    if kind == "error":
                        self._record_error(
                            detail,
                            event_message=message,
                            activity=f"{activity} / {child_phase or 'website inspection'}",
                            child_run_id=child_run_id,
                            child_job_id=job_id,
                        )
                        continue
                    self._event(
                        message,
                        detail=detail,
                        level="item",
                        replace_key=item_replace_key,
                        persist=False,
                    )
                    has_item_activity = True

            def emit_source_errors(errors: list[dict[str, object]]) -> None:
                nonlocal source_error_cursor
                for item in errors:
                    source_error_cursor = max(source_error_cursor, int(item.get("id") or 0))
                    detail = str(item.get("error") or "failed")
                    stage = detail.split(":", 1)[0]
                    processing_failure = stage.startswith((
                        "website scoring", "website checkpoint", "site-owned",
                        "company rescoring", "company-appearance scoring",
                        "saving snapshot", "checkpointing snapshot",
                    ))
                    self._record_error(
                        f"{item.get('source_apex') or activity}: "
                        f"{detail}",
                        event_message=("Company processing failed" if processing_failure
                                       else "A discovery source failed"),
                        activity=(
                            f"{activity} / {stage if processing_failure else 'source discovery'}"
                        ),
                        child_run_id=child_run_id,
                        child_job_id=job_id,
                    )

            def emit_child_actions() -> None:
                nonlocal child_action_cursor, has_item_activity
                for item in list(getattr(job, "actions", ())):
                    action_id = int(item.get("id") or 0)
                    if action_id <= child_action_cursor:
                        continue
                    child_action_cursor = action_id
                    self._action(
                        str(item.get("message") or activity),
                        detail=str(item.get("detail") or ""),
                        level=str(item.get("level") or "item"),
                    )
                    has_item_activity = True

            try:
                while not job.task.done():
                    await asyncio.sleep(0.1)
                    emit_child_actions()
                    runtime_gap = self._runtime_gap_seconds()
                    loop_time = asyncio.get_running_loop().time()
                    probe_due = loop_time >= next_connectivity_probe
                    woke_from_sleep = runtime_gap >= SMART_SCAN_SLEEP_GAP_SECONDS
                    if probe_due or woke_from_sleep:
                        next_connectivity_probe = (
                            loop_time + SMART_SCAN_CONNECTIVITY_PROBE_SECONDS
                        )
                        if not await self._internet_available():
                            job.task.cancel()
                            with suppress(asyncio.CancelledError):
                                await job.task
                            await self._wait_for_internet(
                                reason=(
                                    "Laptop resumed before the network was ready"
                                    if woke_from_sleep
                                    else "Connection dropped during child work"
                                )
                            )
                            return {"status": "reconnect_retry"}
                        if woke_from_sleep:
                            self._event(
                                "Laptop wake detected",
                                detail="Internet is available; continuing the current work item",
                            )
                    activities: list[dict[str, object]] = []
                    source_errors: list[dict[str, object]] = []
                    if loop_time >= next_progress_refresh:
                        next_progress_refresh = loop_time + 0.5
                        progress, child_run_id, activities, source_errors = (
                            await run_thread_owned(
                                read_child_activity, job_id, child_run_id,
                                snapshot_cursor, source_error_cursor,
                            )
                        )
                    child_phase = (
                        str(progress.get("phase") or "working") if progress else "working"
                    )
                    processed = (
                        max(
                            int(progress.get("inspected") or 0),
                            int(progress.get("enriched") or 0),
                            int(progress.get("external_checked") or 0),
                            most_processed,
                        )
                        if progress
                        else most_processed
                    )
                    most_processed = processed
                    if work_units:
                        self._set_work(
                            completed=work_baseline + min(work_units, most_processed),
                            current=f"{activity}: {child_phase.replace('_', ' ')}",
                        )
                        self._persist_state()
                    research_counts = (
                        (int(progress.get("external_checked") or 0),
                         int(progress.get("total_candidates") or 0))
                        if progress and child_phase == "frontier_intelligence" else None
                    )
                    if child_phase != last_phase or (
                        research_counts is not None and research_counts != last_research_counts
                    ):
                        self._event(
                            f"{activity.capitalize()} · {child_phase.replace('_', ' ')}",
                            detail=(
                                f"{research_counts[0]:,}/{research_counts[1]:,} research checks"
                                if research_counts is not None else
                                f"{min(most_processed, work_units):,}/{work_units:,} items"
                                if work_units
                                else "Child stage active"
                            ),
                            replace_key=stage_replace_key,
                        )
                        if not has_item_activity:
                            self._event(
                                "Starting first item",
                                level="item",
                                detail=child_phase.replace("_", " "),
                                replace_key=item_replace_key,
                                persist=False,
                            )
                        last_phase = child_phase
                        last_research_counts = research_counts
                    emit_activities(activities, child_phase)
                    emit_source_errors(source_errors)
                await job.task
            except asyncio.CancelledError:
                if not job.task.done():
                    job.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await job.task
                raise
            child_run_id = int(job.state.get("run_id") or child_run_id)
            emit_child_actions()
            if child_run_id:
                _, _, final_activities, final_source_errors = await run_thread_owned(
                    read_child_activity, job_id, child_run_id,
                    snapshot_cursor, source_error_cursor,
                )
                emit_activities(final_activities, last_phase or "complete")
                emit_source_errors(final_source_errors)
            if work_units:
                self._set_work(completed=work_baseline + work_units, current=activity)
                self._persist_state()
            return dict(job.state)
        return dict(scan_manager.status_for(job_id or None))

    @measure_operation(
        lambda request, **kwargs: request.method, child="scan_run_id", trace_source=True,
    )
    async def _run_discovery_request(
        self,
        request: DiscoveryMethodRunRequest,
        *, independent_sources: bool = False,
    ) -> dict[str, object]:
        request.microlink_enabled = bool(self.state.get("microlink_enabled", True))
        while True:
            if self.state.get("reconnect_pending"):
                await self._wait_for_internet(reason="Resuming public-source work")
            started = (await discovery_method_manager.start(request, independent_sources=True)
                       if independent_sources else await discovery_method_manager.start(request))
            manager = next(
                (
                    item
                    for item in discovery_method_manager.managers.values()
                    if started.get("job_id") and item.state.get("job_id") == started["job_id"]
                ),
                None,
            )
            if manager is not None and manager.task is not None:
                try:
                    previous_progress = None
                    child_job_id = ""
                    child_action_cursor = 0
                    child_phase = ""
                    child_signature = None
                    child_detail = ""
                    method_label = str(_method_tab(request.method)["label"])

                    def report_child_progress(manager=manager, method_label=method_label) -> None:
                        nonlocal child_job_id, child_action_cursor, child_phase
                        nonlocal child_signature, child_detail
                        job_id = str(manager.state.get("scan_job_id") or "")
                        job = scan_manager.jobs.get(job_id) if job_id else None
                        if job is None:
                            return
                        if job_id != child_job_id:
                            child_job_id = job_id
                            child_action_cursor = 0
                            child_phase = ""
                            child_signature = None
                            child_detail = ""
                        changed = False
                        for action in list(job.actions):
                            action_id = int(action.get("id") or 0)
                            if action_id <= child_action_cursor:
                                continue
                            child_action_cursor = action_id
                            child_detail = " · ".join(
                                str(action.get(key) or "") for key in ("message", "detail")
                                if action.get(key)
                            )
                            changed = True
                        with LeadStore(database_path()) as store:
                            progress = store.active_run_progress(job_id)
                        phase = str(progress.get("phase") or child_phase or "working")
                        signature = (
                            phase,
                            progress.get("processed"),
                            progress.get("total_candidates") or progress.get("max_candidates"),
                        )
                        phase_label = phase.replace("_", " ")
                        if phase != child_phase:
                            child_phase = phase
                            self._event(f"{method_label} · {phase_label.capitalize()}")
                        if changed or signature != child_signature:
                            child_signature = signature
                            total = int(progress.get("total_candidates")
                                        or progress.get("max_candidates") or 0)
                            processed = int(progress.get("processed") or 0)
                            counter = (
                                f"{processed:,} / {total:,} checks" if total else
                                f"{processed:,} checks" if progress else ""
                            )
                            self._action(
                                f"{method_label} · {phase_label.capitalize()}",
                                detail=" · ".join(item for item in (counter, child_detail) if item),
                            )

                    while not manager.task.done():
                        self.last_runtime_tick = datetime.now(UTC)
                        report_child_progress()
                        progress = dict(manager.state.get("source_page_progress") or {})
                        if progress and progress != previous_progress:
                            previous_progress = progress
                            self._action(
                                f"Reading source · {progress.get('name')}",
                                detail=(
                                    f"{int(progress.get('pages_fetched') or 0):,} pages read · "
                                    f"{int(progress.get('pages_reused') or 0):,} cached · "
                                    f"{int(progress.get('fetch_attempts') or 0):,} attempts · "
                                    f"{int(progress.get('pages_pending') or 0):,} pages pending"
                                ),
                            )
                        await asyncio.wait({manager.task}, timeout=1)
                    await manager.task
                    report_child_progress()
                except asyncio.CancelledError:
                    manager.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await manager.task
                    raise
                result = manager.status_payload()
            else:
                result = started
            # A bounded source run can legitimately take longer than the wake threshold.
            # Refresh the monotonic checkpoint before the surrounding Smart Scan resumes.
            self.last_runtime_tick = datetime.now(UTC)
            if str(result.get("status")) == "failed" and not await self._internet_available():
                await self._wait_for_internet(
                    reason=f"{request.method.replace('_', ' ')} ended while offline"
                )
                self._event(
                    "Retrying public-source work after reconnection",
                    detail=request.method.replace("_", " "),
                )
                continue
            return result

    @measure_operation("source_research")
    async def _run_source_research(self) -> int:
        if not self.state.get("free_source_scout_complete"):
            try:
                result = await scout_free_sources(
                    database_path(), str(self.state.get("id") or ""),
                    checkpoint=self._checkpoint, event=self._event,
                    search_terms=self.state.get("search_terms", []),
                )
                if result.get("status") != "unavailable":
                    self.state["free_source_scout_complete"] = True
                    self._persist_state()
                self._event("Free source scout finished", detail=(
                    f"{int(result.get('accepted') or 0):,} new sources · {result.get('status')}"
                ))
                for error in result.get("errors", []):
                    self._event("Free source scout warning", level="warn", detail=error)
            except Exception as exc:
                self._event(
                    "Free source scout unavailable", level="warn",
                    detail=f"{type(exc).__name__}; existing sources and AI scout continue",
                )
        if (self.state.get("source_ai_enabled", self.state.get("ai_enabled"))
                and not self.state.get("ai_query_scout_complete")):
            self.state["ai_query_scout_tool_calls_reserved"] = 5 if os.getenv(OPENAI_SECRET) else 0
            self._persist_state()
            try:
                result = await scout_ai_query_sources(
                    database_path(), str(self.state.get("id") or ""),
                    checkpoint=self._checkpoint, event=self._event,
                    search_terms=self.state.get("search_terms", []),
                )
                if result.get("status") != "unavailable":
                    self.state["ai_query_scout_tool_calls_reserved"] = (
                        0 if result.get("status") == "budget_blocked" else 5
                    )
                    self.state["ai_query_scout_complete"] = True
                    self._persist_state()
                self._event("AI query source scout finished", detail=(
                    f"{int(result.get('accepted') or 0):,} new sources · {result.get('status')}"
                ))
                for error in result.get("errors", []):
                    self._event("AI query source scout warning", level="warn", detail=error)
            except Exception as exc:
                self._event("AI query source scout unavailable", level="warn",
                            detail=f"{type(exc).__name__}; existing source research continues")
        query_reserved = int(self.state.get("ai_query_scout_tool_calls_reserved") or 0)
        legacy_cap = min(SMART_SCAN_SCOUT_AI_TOOL_CALLS,
                         SMART_SCAN_SOURCE_AI_TOOL_CALLS - query_reserved)
        with LeadStore(database_path()) as store:
            scout_due = store.source_scout_due()
        if not self.state.get("source_ai_enabled", self.state.get("ai_enabled")):
            self._event("Source scout skipped",
                        detail="Source AI was not selected for this Smart Scan")
            return query_reserved
        if not os.getenv(OPENAI_SECRET):
            self._event(
                "Source scout skipped",
                detail="OpenAI is not configured; free sources continue",
            )
            return query_reserved
        if not scout_due:
            self._event("Source scout reused", detail="The latest source research is still fresh")
            return query_reserved
        while True:
            result = await source_scout_manager.run_inline(
                SourceScoutRequest(
                    max_suggestions=DEFAULT_SCOUT_SUGGESTIONS,
                    max_tool_calls=legacy_cap,
                    keyword_combinations=DEFAULT_SCOUT_KEYWORD_COMBINATIONS,
                ),
                trigger="smart_scan",
            )
            if str(result.get("status")) != "failed" or await self._internet_available():
                break
            await self._wait_for_internet(reason="Source research ended while offline")
            self._event("Retrying source research after reconnection")
        status = str(result.get("status") or "failed")
        if status == "failed":
            self._record_error(str(result.get("message") or "Source scout failed"))
        else:
            self._event(
                "Source scout complete",
                detail=(
                    f"{int(result.get('suggestions') or 0):,} review-only ideas · "
                    f"{legacy_cap} tool calls reserved"
                ),
            )
        return query_reserved + legacy_cap

    async def _run_accepted_sources(self, source_ai_reserved: int) -> None:
        with LeadStore(database_path()) as store:
            accepted = store.list_accepted_discovery_sources()
        remaining_ai = max(0, SMART_SCAN_SOURCE_AI_TOOL_CALLS - source_ai_reserved)
        due_rows = []
        for row in accepted:
            suggestion = _source_suggestion(row)
            if _source_activation_blockers(suggestion):
                continue
            if _source_scan_policy(row)["scan_eligible"]:
                due_rows.append((row, suggestion))
        self.state["source_progress"] = {"completed": 0, "total": len(due_rows)}
        self._event("Accepted source queue mapped", detail=f"{len(due_rows):,} due sources")
        parallelism = 1 if self.state.get("source_ai_enabled", self.state.get("ai_enabled")) else 3
        self._event("Source extraction workers ready", detail=(
            f"Up to {parallelism} independent sources · same-host sources remain serial"
        ))
        refresh_requested = asyncio.Event()
        sources_finished = asyncio.Event()

        async def refresh_inventory():
            # Source slots belong to collection. One owner coalesces completion signals
            # while the full exact recount runs; the phase joins its final publication.
            while True:
                await refresh_requested.wait()
                refresh_requested.clear()
                await self._refresh_discovery_inventory_async()
                if sources_finished.is_set() and not refresh_requested.is_set():
                    return

        async def execute_source(index, row, suggestion):
            nonlocal remaining_ai
            await self._checkpoint()
            method = _method_for_suggestion(suggestion)
            if method is None:
                self._advance_work(current="Skipping unsupported accepted source")
                return
            ai_required = method == "searches"
            ai_enabled = bool(self.state.get("source_ai_enabled", self.state.get("ai_enabled")))
            if ai_required and not ai_enabled:
                self._event(
                    "Saved search deferred",
                    detail="Source AI was not selected for this Smart Scan",
                )
                self._advance_work(current="Saved search deferred")
                return
            if ai_required and remaining_ai <= 0:
                self._event(
                    "Saved search deferred",
                    level="warn",
                    detail="Run-wide source AI ceiling reached",
                )
                self._advance_work(current="Saved search deferred")
                return
            ai_resolution = ai_enabled and remaining_ai > 0
            request = DiscoveryMethodRunRequest(
                method=method,
                suggestion_ids=[int(row["id"])],
                max_pages=AUTO_SOURCE_MAX_PAGES,
                max_candidates=AUTO_SOURCE_MAX_CANDIDATES,
                max_network_inspections=AUTO_SOURCE_MAX_NETWORK_INSPECTIONS,
                ai_resolution=ai_resolution,
                hydrate_companies=False,
                max_ai_tool_calls=1,
                max_ai_reviews=0,
                max_ai_web_reviews=0,
            )
            self._event(
                f"Running accepted source {index:,}/{len(due_rows):,}",
                detail=str(row.get("name") or suggestion.name),
            )
            self._action(
                f"Source starting · {row.get('name') or suggestion.name}",
                detail=method.replace("_", " "),
            )
            try:
                result = await self._run_discovery_request(
                    request, independent_sources=parallelism > 1)
                refresh_requested.set()
                result_status = str(result.get("status") or "failed")
                if result_status == "partial":
                    self._record_error(
                        f"{row.get('name') or suggestion.name}: "
                        + str(
                            result.get('error') or result.get('message') or 'partial source result'
                        ),
                        activity=f"{method} collection / {suggestion.name}",
                        child_run_id=int(result.get("run_id") or 0),
                        child_job_id=str(result.get("job_id") or ""),
                    )
                elif result_status != "complete":
                    self._record_error(
                        f"{row.get('name') or suggestion.name}: "
                        f"{result.get('message') or result.get('status')}",
                        activity=f"{method} collection / {suggestion.name}",
                        child_run_id=int(result.get("run_id") or 0),
                        child_job_id=str(result.get("job_id") or ""),
                    )
                if ai_resolution:
                    remaining_ai -= 1
                counters = dict(self.state.get("counters") or {})
                counters["source_runs_completed"] = int(
                    counters.get("source_runs_completed") or 0
                ) + 1
                self.state["counters"] = counters
                item_result = _source_run_result(row, result) or {}
                coverage = dict(item_result.get("coverage") or {})
                slice_only = bool(coverage) and not coverage.get("complete")
                suffix = (" · continuation remains due" if coverage.get("continuation") else
                          " · observed scope only" if slice_only else "")
                reused = int(coverage.get("pages_reused") or 0)
                if item_result.get("enumeration_seconds") is not None:
                    suffix += f" · {float(item_result['enumeration_seconds']):.1f}s extraction"
                if reused:
                    suffix += f" · {reused:,} cached pages reused"
                self._event(
                    f"Accepted source{' slice' if slice_only else ''} "
                    f"{index:,}/{len(due_rows):,} finished",
                    level="success" if str(result.get("status")) == "complete" else "warn",
                    detail=str(row.get("name") or suggestion.name) + suffix,
                )
                self._action(
                    f"Source finished · {row.get('name') or suggestion.name}",
                    detail=result_status.replace("_", " "),
                    level="error" if result_status == "partial" else "item",
                )
            except Exception as exc:
                self._record_error(
                    f"{row.get('name') or suggestion.name}: {type(exc).__name__}: {exc}",
                    activity=f"{method} collection / {suggestion.name}",
                )
            self._advance_work(current=str(row.get("name") or suggestion.name))

        pending = list(enumerate(due_rows, start=1))
        active_hosts = set()
        host_released = asyncio.Event()
        completed = 0

        async def worker():
            nonlocal completed
            while pending:
                selected = next(
                    (
                        position
                        for position, (_, (_, suggestion)) in enumerate(pending)
                        if (urlsplit(suggestion.url).hostname or suggestion.apex or "")
                        .lower()
                        .removeprefix("www.")
                        not in active_hosts
                    ),
                    None,
                )
                if selected is None:
                    host_released.clear()
                    await host_released.wait()
                    continue
                index, (row, suggestion) = pending.pop(selected)
                host = (
                    (urlsplit(suggestion.url).hostname or suggestion.apex or "")
                    .lower()
                    .removeprefix("www.")
                )
                active_hosts.add(host)
                try:
                    await execute_source(index, row, suggestion)
                    completed += 1
                    self.state["source_progress"] = {"completed": completed, "total": len(due_rows)}
                    self._persist_state()
                finally:
                    active_hosts.discard(host)
                    host_released.set()

        from .async_work import cancel_and_drain

        refresh_task = asyncio.create_task(refresh_inventory())
        workers = [asyncio.create_task(worker()) for _ in range(min(parallelism, len(due_rows)))]
        try:
            await asyncio.gather(*workers)
            sources_finished.set()
            refresh_requested.set()
            await refresh_task
        finally:
            await cancel_and_drain([*workers, refresh_task])
        self.state["source_progress"] = {"completed": completed, "total": len(due_rows)}

    @measure_operation("rss_sources")
    async def _run_rss_sources(self) -> None:
        """Discover domains before the existing company-hydration boundary is frozen."""
        completed = set(self.state.get("rss_sources_done") or [])
        window_end = datetime.fromisoformat(str(self.state["started_at"]).replace("Z", "+00:00"))
        self.state["rss_progress"] = {"completed": len(completed), "total": len(RSS_SOURCES)}
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for source in RSS_SOURCES:
                if source.url in completed:
                    continue
                await self._checkpoint()
                self.state["message"] = f"Reading RSS source · {source.name}"
                self._persist_state()
                self._event("Reading RSS aggregator", detail=source.name)
                self._action(f"RSS discovery · {source.name}", detail="Last 7 days · free")

                def cache_get(host, kind):
                    with source_time("rss_cache_read"), LeadStore(database_path()) as store:
                        return store.get_free_signal_cache(host, kind)

                def cache_set(host, kind, payload, ttl):
                    with source_time("rss_cache_write"), LeadStore(database_path()) as store:
                        store.set_free_signal_cache(host, kind, payload, ttl)

                def save(candidates):
                    if not candidates:
                        return
                    with source_time("rss_candidate_write"), LeadStore(database_path()) as store:
                        save_rss_candidates(store, candidates)

                async def checkpoint(source_name=source.name):
                    await self._checkpoint()
                    self._action(
                        f"RSS discovery · {source_name}", detail="Extracting article domains"
                    )

                try:
                    with source_trace("rss", source=source.name) as metrics:
                        result = await collect_rss_source(
                            source, window_end=window_end, fetch=RSSFetcher(client, source),
                            cache_get=cache_get, cache_set=cache_set, save=save,
                            checkpoint=checkpoint,
                        )
                        if metrics is not None:
                            metrics.result.update(
                                articles=result.articles, skipped=result.skipped,
                                cache_hits=result.cache_hits,
                                deferred_articles=result.deferred_articles,
                                unique_candidates=len({x.hostname for x in result.candidates}),
                                errors=len(result.errors),
                            )
                    for error in result.errors:
                        self._record_error(
                            f"RSS {source.name}: {error}",
                            activity=f"RSS collection / {source.name} / article extraction",
                        )
                    counters = dict(self.state.get("counters") or {})
                    counters["rss_articles_processed"] = int(
                        counters.get("rss_articles_processed") or 0
                    ) + result.articles
                    counters["rss_article_cache_hits"] = int(
                        counters.get("rss_article_cache_hits") or 0
                    ) + result.cache_hits
                    counters["rss_articles_deferred"] = int(
                        counters.get("rss_articles_deferred") or 0
                    ) + result.deferred_articles
                    self.state["counters"] = counters
                    self._event("RSS aggregator finished", detail=(
                        f"{source.name} · {result.articles:,} recent articles · "
                        f"{len({item.hostname for item in result.candidates}):,} unique domains · "
                        f"{result.skipped:,} old, undated or future items skipped · "
                        + (f"{result.deferred_articles:,} article pages deferred · "
                           if result.deferred_articles else "")
                        + "coverage limited to supplied feed items"
                    ))
                except Exception as exc:
                    self._record_error(
                        f"RSS {source.name}: {type(exc).__name__}: {exc}",
                        activity=f"RSS collection / {source.name} / feed and article extraction",
                    )
                completed.add(source.url)
                self.state["rss_sources_done"] = sorted(completed)
                self.state["rss_progress"] = {
                    "completed": len(completed), "total": len(RSS_SOURCES),
                }
                self._advance_work(current=f"RSS · {source.name}")
                self._persist_state()

    async def _run_free_source_method(
        self, method: str, *, hydrate_companies: bool = True,
    ) -> str:
        await self._checkpoint()
        label = str(_method_tab(method)["label"])
        self.state["message"] = f"Running {label.lower()}"
        self._persist_state()
        self._event(f"Running {label.lower()}")
        self._action(f"Source starting · {label}", detail=method.replace("_", " "))
        outcome = "error"
        try:
            result = await self._run_discovery_request(
                DiscoveryMethodRunRequest(
                    method=method,
                    max_pages=1,
                    max_candidates=500,
                    max_network_inspections=500,
                    ai_resolution=False,
                    max_ai_tool_calls=1,
                    max_ai_reviews=0,
                    max_ai_web_reviews=0,
                    hydrate_companies=hydrate_companies,
                    people_hydration_enabled=False,
                )
            )
            result_status = str(result.get("status") or "failed")
            outcome = (
                "database_locked"
                if result_status == "failed" and "database is locked" in str(
                    result.get("error") or result.get("message") or ""
                ).lower()
                else result_status
            )
            if result_status == "partial":
                self._record_error(
                    f"{label}: "
                    f"{result.get('error') or result.get('message') or 'partial source result'}"
                )
            elif result_status != "complete":
                self._record_error(f"{label}: {result.get('message') or result.get('status')}")
            counters = dict(self.state.get("counters") or {})
            counters["source_runs_completed"] = int(
                counters.get("source_runs_completed") or 0
            ) + 1
            self.state["counters"] = counters
            self._event(
                f"{label} finished",
                level="success" if str(result.get("status")) == "complete" else "warn",
            )
            self._action(
                f"Source finished · {label}",
                detail=result_status.replace("_", " "),
                level="error" if result_status == "partial" else "item",
            )
        except Exception as exc:
            self._record_error(f"{label}: {type(exc).__name__}: {exc}")
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
                outcome = "database_locked"
        if outcome != "database_locked":
            self._advance_work(current=label)
        return outcome


    @staticmethod
    def _read_candidate_boundary() -> int:
        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            return int(store.connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0])

    async def _run_free_source_methods(self) -> None:
        # These methods own website child scans; keep one foreground hydration lane.
        await self._run_free_source_method("public_graph")
        with LeadStore(database_path()) as store:
            sources = store.enabled_sources()
            maximum = store.scan_capacity(sources, MEGA_MAX_HOSTS_PER_SOURCE)
        if not sources:
            self._event(
                "Web & CT source scan skipped",
                detail="No generic managed apexes are enabled",
            )
            self._advance_work(current="Web & CT source scan skipped")
            return
        self.state["message"] = "Running enabled Web & CT sources"
        self._persist_state()
        self._event("Running enabled Web & CT sources", detail=f"{len(sources):,} apexes")
        try:
            result = await self._wait_for_scan(
                ScanRequest(
                    sources=sources,
                    max_hosts_per_source=MEGA_MAX_HOSTS_PER_SOURCE,
                    max_network_inspections=SMART_SCAN_HYDRATION_BATCH_SIZE,
                    adaptive_refresh=True,
                    concurrency=30,
                    pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
                    english_only=True,
                    free_intelligence=True,
                    max_sitemaps_per_site=4,
                    max_common_crawl_lookups=200,
                    max_news_lookups=50,
                    max_frontier_scans=SMART_SCAN_MAX_FRONTIER_SCANS,
                    frontier_scanner_ids=list(FRONTIER_SCANNER_IDS),
                    respect_frontier_schedule=True,
                    agency_discovery=False,
                    ai=False,
                    ai_web=False,
                    max_ai_calls=0,
                    max_ai_web_calls=0,
                    evergreen_discovery=False,
                ),
                sources,
                maximum,
            )
            if str(result.get("status")) not in {"complete", "partial"}:
                self._record_error(f"Web & CT: {result.get('message') or result.get('status')}")
        except Exception as exc:
            self._record_error(f"Web & CT: {type(exc).__name__}: {exc}")
        self._advance_work(current="Web & CT sources")

    @measure_operation("web_ct_discovery")
    async def _run_web_ct_discovery_only(self) -> None:
        """Persist bounded enabled-apex discoveries for the next hydration boundary."""
        with LeadStore(database_path()) as store:
            sources = store.enabled_sources()
        if not sources:
            self._event("Web & CT discovery skipped", detail="No enabled apexes")
            self._advance_work(current="Web & CT discovery skipped")
            return

        def start_run():
            with LeadStore(database_path()) as store:
                return store.start_source_execution_run(
                    "web_ct", "web_ct", [],
                    {"discovery_only": True, "max_hosts_per_source": MEGA_MAX_HOSTS_PER_SOURCE},
                    job_id=uuid.uuid4().hex[:12],
                )

        created_run_id = None

        def owned_start():
            nonlocal created_run_id
            created_run_id = start_run()
            return created_run_id

        try:
            run_id = await _run_market_database_work(owned_start)
        except asyncio.CancelledError:
            if created_run_id is not None:
                await _run_market_database_work(partial(
                    _market_store_call, "finish_source_execution_run", created_run_id,
                    "cancelled", {"sources_completed": 0, "discovery_only": True},
                ))
            raise
        errors: list[str] = []
        completed = 0
        found_total = 0
        self._event("Discovering enabled Web & CT sources", detail=f"{len(sources):,} apexes")

        async def one(apex: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore):
            nonlocal completed, found_total
            async with semaphore:
                await self._checkpoint()
                try:
                    found = await discover_apex(
                        client, apex, max_hosts=MEGA_MAX_HOSTS_PER_SOURCE,
                    )
                    await _run_market_database_work(partial(
                        _market_store_call, "upsert_candidates", found,
                    ))
                    found_total += len(found)
                    self._action(f"Discovered {apex}", detail=f"{len(found):,} candidates saved")
                except (httpx.HTTPError, DiscoveryError, ValueError, sqlite3.Error) as exc:
                    errors.append(f"{apex}: {type(exc).__name__}: {exc}")
                    self._record_error(f"Web & CT {apex}: {type(exc).__name__}: {exc}")
                completed += 1
                await _run_market_database_work(partial(
                    _market_store_call, "update_source_execution_run", run_id,
                    sources_total=len(sources), sources_completed=completed,
                    direct_candidates=found_total, candidates_persisted=found_total,
                ))

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
                headers={"User-Agent": USER_AGENT}, trust_env=False,
            ) as client:
                semaphore = asyncio.Semaphore(3)
                async with asyncio.TaskGroup() as group:
                    for apex in sources:
                        group.create_task(one(apex, client, semaphore))
        except asyncio.CancelledError:
            await _run_market_database_work(partial(
                _market_store_call, "finish_source_execution_run", run_id,
                "cancelled", {"sources_completed": completed, "errors": errors[:50]},
            ))
            raise
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            await _run_market_database_work(partial(
                _market_store_call, "finish_source_execution_run", run_id,
                "failed", {"sources_completed": completed, "errors": errors[:50]},
            ))
            raise
        status = "partial" if errors else "complete"
        await _run_market_database_work(partial(
            _market_store_call, "finish_source_execution_run", run_id,
            status, {"sources_total": len(sources), "sources_completed": completed,
                     "direct_candidates": found_total, "errors": errors[:50],
                     "discovery_only": True},
        ))
        if any("database is locked" in error.lower() for error in errors):
            # Candidate publication is idempotent. Retry this bounded source pass
            # after the company writer drains rather than declaring it complete.
            raise sqlite3.OperationalError("database is locked")
        self._event("Web & CT discovery finished", detail=(
            f"{completed:,} apexes · {found_total:,} candidate records saved"
        ))
        counters = dict(self.state.get("counters") or {})
        counters["source_runs_completed"] = int(
            counters.get("source_runs_completed") or 0
        ) + 1
        self.state["counters"] = counters
        self._advance_work(current="Web & CT discovery")

    @staticmethod
    def _hydration_request(hosts: list[str]) -> ScanRequest:
        return ScanRequest(
            sources=[],
            only_hosts=hosts,
            max_hosts_per_source=len(hosts),
            max_network_inspections=len(hosts),
            adaptive_refresh=True,
            concurrency=30,
            pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
            english_only=True,
            free_intelligence=True,
            max_sitemaps_per_site=4,
            max_common_crawl_lookups=min(200, len(hosts)),
            max_news_lookups=min(50, len(hosts)),
            max_frontier_scans=SMART_SCAN_MAX_FRONTIER_SCANS,
            frontier_scanner_ids=list(FRONTIER_SCANNER_IDS),
            respect_frontier_schedule=True,
            agency_discovery=False,
            ai=False,
            ai_web=False,
            max_ai_calls=0,
            max_ai_web_calls=0,
            evergreen_discovery=False,
        )

    def _account_hydration_result(
        self,
        result: Mapping[str, object],
        *,
        recovered: bool = False,
    ) -> None:
        counters = dict(self.state.get("counters") or {})
        counters["hydration_batches_completed"] = int(
            counters.get("hydration_batches_completed") or 0
        ) + 1
        if recovered:
            counters["hydration_batches_recovered"] = int(
                counters.get("hydration_batches_recovered") or 0
            ) + 1
        run_id = int(result.get("run_id") or 0)
        if run_id:
            try:
                with LeadStore(database_path()) as store:
                    summary = dict(store.run_metadata(run_id).get("summary") or {})
                counters["hydration_network_scanned"] = int(
                    counters.get("hydration_network_scanned") or 0
                ) + int(summary.get("candidates_network_scanned") or 0)
                counters["hydration_reused"] = int(
                    counters.get("hydration_reused") or 0
                ) + int(
                    summary.get(
                        "candidates_reused_within_90d",
                        summary.get(
                            "candidates_reused_within_30d",
                            summary.get("candidates_reused_within_24h", 0),
                        ),
                    )
                    or 0
                )
            except sqlite3.Error:
                logger.exception("failed to load hydration summary run_id=%d", run_id)
        self.state["counters"] = counters

    @staticmethod
    def _child_failure_detail(result: Mapping[str, object]) -> str:
        return str(
            result.get("error")
            or result.get("message")
            or result.get("status")
            or "child scan failed"
        )

    @measure_operation("base_hydration")
    async def _run_base_hydration(self, *, after_candidate_id: int = 0) -> None:
        from .async_work import run_thread_owned

        scope_after = int(self.state.get("hydration_scope_after") or 0)
        scope_matches = scope_after == after_candidate_id
        frozen = self.state.get("candidate_boundary") if scope_matches else None
        stage = dict(self.state.get("hydration_progress") or {}).get("stage")
        if frozen is not None and stage == "complete":
            return
        if frozen is not None and stage == "scanners":
            await self._run_scanner_gap_hydration(
                int(frozen), after_candidate_id=after_candidate_id,
            )
            return
        def plan():
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                store.connection.execute("BEGIN")
                boundary = int(
                    store.connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM candidates"
                    ).fetchone()[0]
                )
                saved_limit = frozen
                if after_candidate_id and saved_limit is None:
                    saved_limit = self.state.get("market_candidate_boundary")
                if saved_limit is not None:
                    boundary = min(boundary, int(saved_limit))
                inventory = store.smart_scan_inventory(
                    max_candidate_id=boundary,
                    max_company_failure_rate=self.state.get("max_company_failure_rate"),
                )
                due_inventory = store.smart_scan_inventory(
                    max_candidate_id=boundary, after_candidate_id=after_candidate_id,
                    max_company_failure_rate=self.state.get("max_company_failure_rate"),
                ) if after_candidate_id else inventory
            return boundary, inventory, int(due_inventory.get("due_candidates") or 0)

        with source_trace("refresh_plan") as metrics:
            boundary, inventory, due_total = await asyncio.to_thread(plan)
            if metrics is not None:
                metrics.result.update(due_companies=due_total, candidate_boundary=boundary)
        self.state["inventory"] = {**dict(self.state.get("inventory") or {}), **inventory}
        self.state["candidate_boundary"] = boundary
        self.state["hydration_scope_after"] = after_candidate_id
        self._persist_state()
        self._event(
            "Hydration boundary frozen",
            detail=f"{due_total:,} due records through candidate {boundary:,}",
        )
        cursor = max(0, int(after_candidate_id))
        batch_number = 0
        submitted = 0
        prior_submitted = int(
            (self.state.get("counters") or {}).get("hydration_hosts_submitted") or 0
        )
        deferred_batches: list[tuple[int, list[str], str]] = []
        failure_limit = self.state.get("max_company_failure_rate")

        def read_due_page(after_id):
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                store.connection.execute("BEGIN")
                return store.smart_scan_due_candidates(
                    after_candidate_id=after_id, max_candidate_id=boundary,
                    limit=SMART_SCAN_HYDRATION_BATCH_SIZE, max_company_failure_rate=failure_limit,
                )

        while cursor < boundary:
            await self._checkpoint()
            with source_trace("refresh_due_page", after_candidate_id=cursor) as metrics:
                page = await run_thread_owned(read_due_page, cursor)
                if metrics is not None:
                    metrics.result.update(companies=len(page["items"]), exhausted=page["exhausted"])
            next_cursor = int(page["next_candidate_id"])
            candidates = list(page["items"])
            if next_cursor <= cursor and not page["exhausted"]:
                self._record_error("Hydration cursor did not advance")
                break
            cursor = next_cursor
            self.state["candidate_cursor"] = cursor
            if not candidates:
                if page["exhausted"]:
                    break
                continue
            batch_number += 1
            hosts = [candidate.hostname for candidate in candidates]
            submitted += len(hosts)
            self.state["hydration_progress"] = {
                "submitted": submitted,
                "due_at_boundary": due_total,
                "batch": batch_number,
                "stage": "core",
            }
            self._event(
                f"Hydration batch {batch_number:,} running",
                detail=f"{submitted:,}/{due_total:,} due records submitted",
                replace_key="hydration-progress",
            )
            try:
                result = await self._wait_for_scan(
                    self._hydration_request(hosts),
                    [],
                    len(hosts),
                    work_units=len(hosts),
                    activity="company hydration",
                )
                counters = dict(self.state.get("counters") or {})
                counters["hydration_hosts_submitted"] = prior_submitted + submitted
                self.state["counters"] = counters
                if str(result.get("status")) in {"complete", "partial"}:
                    self._account_hydration_result(result)
                else:
                    detail = self._child_failure_detail(result)
                    deferred_batches.append((batch_number, hosts, detail))
                    counters = dict(self.state.get("counters") or {})
                    counters["hydration_batches_deferred"] = len(deferred_batches)
                    counters["hydration_hosts_deferred"] = sum(
                        len(batch_hosts) for _, batch_hosts, _ in deferred_batches
                    )
                    self.state["counters"] = counters
                    self._event(
                        f"Hydration batch {batch_number:,} deferred for one replay",
                        level="warn",
                        detail=detail,
                    )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                deferred_batches.append((batch_number, hosts, detail))
                counters = dict(self.state.get("counters") or {})
                counters["hydration_batches_deferred"] = len(deferred_batches)
                counters["hydration_hosts_deferred"] = sum(
                    len(batch_hosts) for _, batch_hosts, _ in deferred_batches
                )
                self.state["counters"] = counters
                self._event(
                    f"Hydration batch {batch_number:,} deferred for one replay",
                    level="warn",
                    detail=detail,
                )
            if page["exhausted"]:
                break
        for original_batch, hosts, original_detail in deferred_batches:
            await self._checkpoint()
            self._event(
                f"Replaying deferred hydration batch {original_batch:,}",
                detail=f"{len(hosts):,} records · prior failure: {original_detail}",
            )
            try:
                result = await self._wait_for_scan(
                    self._hydration_request(hosts),
                    [],
                    len(hosts),
                    work_units=len(hosts),
                    activity="deferred company hydration",
                )
                if str(result.get("status")) in {"complete", "partial"}:
                    self._account_hydration_result(result, recovered=True)
                    self._event(
                        f"Deferred hydration batch {original_batch:,} recovered",
                        level="success",
                        detail=f"{len(hosts):,} records resubmitted through durable claims",
                    )
                    continue
                detail = self._child_failure_detail(result)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            counters = dict(self.state.get("counters") or {})
            counters["hydration_batches_unrecovered"] = int(
                counters.get("hydration_batches_unrecovered") or 0
            ) + 1
            counters["hydration_hosts_unrecovered"] = int(
                counters.get("hydration_hosts_unrecovered") or 0
            ) + len(hosts)
            self.state["counters"] = counters
            self._record_error(
                f"Hydration batch {original_batch} replay failed: {detail}"
            )
        self._event(
            "Late-source hydration pass complete" if after_candidate_id
            else "Full-base hydration pass complete",
            detail=f"{submitted:,} due records submitted in {batch_number:,} batches",
        )
        self.state["hydration_progress"] = {
            **dict(self.state.get("hydration_progress") or {}),
            "stage": "scanners", "candidate_boundary": boundary,
            "scanner_cursor": after_candidate_id,
        }
        self._persist_state()
        if after_candidate_id:
            await self._run_scanner_gap_hydration(boundary, after_candidate_id=after_candidate_id)
        else:
            await self._run_scanner_gap_hydration(boundary)

    @measure_operation("scanner_gap_hydration")
    async def _run_scanner_gap_hydration(
        self, boundary: int, *, after_candidate_id: int = 0,
    ) -> None:
        """Fill scanner-specific gaps using only core snapshots reusable inside 90 days."""

        scanner_versions = [
            (definition.scanner_id, definition.version)
            for definition in SCANNER_DEFINITIONS
        ]
        if not scanner_versions or boundary <= 0:
            return
        # A 25-company page leaves even an all-scanner schedule below the hard 500-item
        # child-run limit. Candidates are then grouped by their exact due scanner set so a newly
        # activated method never reopens fresh sibling methods.
        if SMART_SCAN_SCANNER_BATCH_SIZE * len(scanner_versions) > SMART_SCAN_MAX_FRONTIER_SCANS:
            raise RuntimeError("Smart Scan scanner batch exceeds the frontier work ceiling")
        self._event(
            "Mapping scanner-specific gaps",
            detail=(
                f"{len(scanner_versions):,} free scanners · "
                f"{SMART_SCAN_SCANNER_BATCH_SIZE:,} reusable companies per batch"
            ),
        )
        cursor = max(0, int(after_candidate_id))
        batch_number = 0
        submitted = 0
        prior_counters = dict(self.state.get("counters") or {})
        prior_batches = int(prior_counters.get("scanner_batches_completed") or 0)
        prior_hosts = int(prior_counters.get("scanner_hosts_submitted") or 0)
        while cursor < boundary:
            await self._checkpoint()
            with LeadStore(database_path()) as store:
                page = store.smart_scan_scanner_gap_candidates(
                    scanner_versions,
                    after_candidate_id=cursor,
                    max_candidate_id=boundary,
                    limit=SMART_SCAN_SCANNER_BATCH_SIZE,
                    max_company_failure_rate=self.state.get(
                        "max_company_failure_rate"
                    ),
                )
            next_cursor = int(page["next_candidate_id"])
            candidates = list(page["items"])
            if next_cursor <= cursor and not page["exhausted"]:
                self._record_error("Scanner-gap cursor did not advance")
                break
            cursor = next_cursor
            self.state["hydration_progress"] = {
                "stage": "scanners",
                "scanner_cursor": cursor,
                "candidate_boundary": boundary,
            }
            if not candidates:
                if page["exhausted"]:
                    break
                continue
            scanner_ids_by_hostname = {
                str(hostname): tuple(str(scanner_id) for scanner_id in scanner_ids)
                for hostname, scanner_ids in dict(
                    page.get("scanner_ids_by_hostname") or {}
                ).items()
            }
            selected = [item for item in candidates if scanner_ids_by_hostname.get(item.hostname)]
            union = tuple(dict.fromkeys(
                scanner for item in selected for scanner in scanner_ids_by_hostname[item.hostname]
            ))
            grouped_candidates = {union: selected} if selected else {}
            submitted += sum(len(group) for group in grouped_candidates.values())
            work = dict(self.state.get("work") or {})
            self._set_work(
                total=int(work.get("total") or 0)
                + sum(len(group) for group in grouped_candidates.values())
            )
            self._persist_state()
            for due_scanner_ids, candidate_group in grouped_candidates.items():
                batch_number += 1
                hosts = [candidate.hostname for candidate in candidate_group]
                self._event(
                    f"Scanner-gap batch {batch_number:,} running",
                    detail=(
                        f"{submitted:,} reusable companies submitted · "
                        f"{', '.join(due_scanner_ids)} only"
                    ),
                    replace_key="scanner-gap-progress",
                )
                try:
                    result = await self._wait_for_scan(
                        ScanRequest(
                            sources=[],
                            only_hosts=hosts,
                            max_hosts_per_source=len(hosts),
                            max_network_inspections=0,
                            reuse_core_only=True,
                            adaptive_refresh=True,
                            concurrency=20,
                            pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
                            english_only=True,
                            free_intelligence=True,
                            max_sitemaps_per_site=4,
                            max_common_crawl_lookups=0,
                            max_news_lookups=0,
                            max_frontier_scans=min(
                                SMART_SCAN_MAX_FRONTIER_SCANS,
                                len(hosts) * len(due_scanner_ids),
                            ),
                            frontier_scanner_ids=list(due_scanner_ids),
                            frontier_scanner_manifest={
                                host: list(scanner_ids_by_hostname[host]) for host in hosts
                            },
                            respect_frontier_schedule=True,
                            agency_discovery=False,
                            ai=False,
                            ai_web=False,
                            max_ai_calls=0,
                            max_ai_web_calls=0,
                            evergreen_discovery=False,
                        ),
                        [],
                        len(hosts),
                        work_units=len(hosts),
                        activity="scanner coverage",
                    )
                    counters = dict(self.state.get("counters") or {})
                    counters["scanner_batches_completed"] = prior_batches + batch_number
                    counters["scanner_hosts_submitted"] = prior_hosts + submitted
                    run_id = result.get("run_id")
                    if run_id:
                        with LeadStore(database_path()) as store:
                            summary = dict(
                                store.run_metadata(int(run_id)).get("summary") or {}
                            )
                        frontier = dict(summary.get("frontier_intelligence") or {})
                        counters["scanner_executions_completed"] = int(
                            counters.get("scanner_executions_completed") or 0
                        ) + int(frontier.get("optional_completed") or 0) + int(
                            frontier.get("embedded_completed") or 0
                        )
                        counters["scanner_executions_reused"] = int(
                            counters.get("scanner_executions_reused") or 0
                        ) + int(frontier.get("optional_reused") or 0) + int(
                            frontier.get("embedded_reused") or 0
                        )
                    self.state["counters"] = counters
                    if str(result.get("status")) not in {"complete", "partial"}:
                        self._record_error(
                            f"Scanner-gap batch {batch_number}: "
                            f"{result.get('message') or result.get('status')}"
                        )
                except Exception as exc:
                    self._record_error(
                        f"Scanner-gap batch {batch_number}: {type(exc).__name__}: {exc}"
                    )
            self.state["scanner_candidate_cursor"] = cursor
            self._persist_state()
            if page["exhausted"]:
                break
        self.state["hydration_progress"] = {
            "stage": "complete",
            "scanner_cursor": boundary,
            "candidate_boundary": boundary,
        }
        self._event(
            "Scanner-gap pass complete",
            detail=f"{submitted:,} companies submitted in {batch_number:,} batches",
        )

    async def _record_dataforseo_result(
        self,
        result: object,
        *,
        stage: str,
        candidates_by_domain: dict[str, dict[str, object]],
    ) -> tuple[int, int]:
        """Publish one paid response on a single worker without blocking scan controls."""

        scan_id = "".join(
            character
            for character in str(self.state.get("id") or "").lower()
            if character.isalnum()
        )[:32]
        source_id = f"dataforseo:{scan_id}:{stage}"
        database = database_path()
        loop = asyncio.get_running_loop()

        def report(message: str, detail: str, level: str = "item") -> None:
            # SQLite and score computation belong to the worker; UI state has one owner.
            loop.call_soon_threadsafe(
                lambda: self._action(message, detail=detail, level=level)
            )

        def publish() -> tuple[int, int, list[str]]:
            persisted: list[tuple[int, str]] = []
            projection_errors: list[str] = []
            with LeadStore(database) as store:
                for item in getattr(result, "items", ()):
                    domain = seo_registered_domain(str(item.get("target") or ""))
                    candidate = candidates_by_domain.get(domain)
                    if candidate is None:
                        projection_errors.append(
                            f"DataForSEO returned an unresolved {stage} target"
                        )
                        report(
                            f"SEO unresolved · {domain or 'unknown target'}",
                            stage.replace("_", " "), "error",
                        )
                        continue
                    candidate_id = int(candidate["candidate_id"])
                    company_domain = str(candidate["canonical_domain"])
                    try:
                        projection = project_dataforseo_item(
                            item,
                            stage=stage,
                            company_domain=company_domain,
                            task_id=str(getattr(result, "task_id", "") or ""),
                            response_hash=str(getattr(result, "response_hash", "") or ""),
                        )
                        store.record_market_projection(
                            candidate_id, company_domain, source_id, projection,
                        )
                        persisted.append((candidate_id, domain))
                        report(f"SEO committed · {domain}", stage.replace("_", " "))
                    except (DataForSEOError, ValueError) as exc:
                        projection_errors.append(
                            f"DataForSEO {stage} projection for {domain}: {exc}"
                        )
                        report(f"SEO failed · {domain}", str(exc), "error")
                reprojected = len(reproject_candidates(
                    store,
                    [candidate_id for candidate_id, _ in persisted],
                ))
                for index, (_, domain) in enumerate(persisted, start=1):
                    report(
                        f"SEO scored · {domain}",
                        f"{index:,}/{len(persisted):,} returned companies scored",
                    )
            return len(persisted), reprojected, projection_errors

        persisted, reprojected, projection_errors = await self._await_seo_publication(publish)
        for message in projection_errors:
            self._record_error(message)
        return persisted, reprojected

    async def _await_seo_publication(self, publish: Callable[[], Any]) -> Any:
        """Keep one writer owned by the scan until its complete response is committed."""
        worker = asyncio.create_task(asyncio.to_thread(publish))
        try:
            while not worker.done():
                await asyncio.wait({worker}, timeout=1)
                self.last_runtime_tick = datetime.now(UTC)
            return worker.result()
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Drain it before releasing scan ownership,
            # preserving all returned paid evidence and preventing an orphan writer.
            with suppress(Exception):
                await asyncio.shield(worker)
            raise

    async def _record_dataforseo_details(
        self, batch: list[dict[str, object]], outcomes: list[object],
        candidates_by_domain: dict[str, dict[str, object]],
    ) -> None:
        """Own every already-returned paid response through a detail-group cancellation."""
        async def publish() -> None:
            cancelled = False
            for item, outcome in zip(batch, outcomes, strict=True):
                if isinstance(outcome, asyncio.CancelledError):
                    cancelled = True
                    continue
                if isinstance(outcome, BaseException):
                    counters = dict(self.state.get("counters") or {})
                    counters["dataforseo_failures"] = int(
                        counters.get("dataforseo_failures") or 0
                    ) + 1
                    self.state["counters"] = counters
                    message = (
                        str(outcome) if isinstance(outcome, DataForSEOError)
                        else "DataForSEO detail request could not be completed"
                    )
                    self._record_error(
                        f"DataForSEO detail for {item['canonical_domain']}: {message}"
                    )
                    self._action(
                        f"SEO detail failed · {item['canonical_domain']}",
                        detail=message, level="error",
                    )
                    continue
                self._add_dataforseo_cost(outcome.cost_usd)
                self._persist_state()
                persisted, reprojected = await self._record_dataforseo_result(
                    outcome, stage="domain_overview", candidates_by_domain=candidates_by_domain,
                )
                counters = dict(self.state.get("counters") or {})
                counters["dataforseo_detail_returned"] = int(
                    counters.get("dataforseo_detail_returned") or 0
                ) + persisted
                counters["dataforseo_reprojected"] = int(
                    counters.get("dataforseo_reprojected") or 0
                ) + reprojected
                self.state["counters"] = counters
                self._persist_state()
            if cancelled:
                raise asyncio.CancelledError

        publication = asyncio.create_task(publish())
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await asyncio.shield(publication)
            raise

    def _add_dataforseo_cost(self, cost: Decimal) -> None:
        current = Decimal(str(self.state.get("actual_dataforseo_cost_usd") or 0))
        self.state["actual_dataforseo_cost_usd"] = float(
            (current + cost).quantize(Decimal("0.000001"))
        )
        record_spending_event(cost=cost, operation=str(self.state.get("id") or ""))

    def _reserve_dataforseo_requests(self, count: int) -> bool:
        counters = dict(self.state.get("counters") or {})
        requested = int(counters.get("dataforseo_requests") or 0)
        amount = max(0, int(count))
        request_cap = int(
            self.state.get("dataforseo_request_cap")
            or counters.get("dataforseo_request_cap")
            or 0
        )
        if request_cap <= 0 or requested + amount > request_cap:
            return False
        counters["dataforseo_requests"] = requested + amount
        self.state["counters"] = counters
        self._persist_state()
        return True

    @measure_operation("dataforseo")
    async def _run_dataforseo_correction(self) -> None:
        counters = dict(self.state.get("counters") or {})
        requested_site_limit = int(
            self.state.get("seo_site_limit")
            or counters.get("dataforseo_target_cap")
            or 2_500
        )
        site_limit, detail_limit, request_cap = _smart_scan_dataforseo_limits(
            requested_site_limit
        )
        self.state["seo_site_limit"] = site_limit if self.state.get("dataforseo_enabled") else 0
        self.state["dataforseo_detail_limit"] = (
            detail_limit if self.state.get("dataforseo_enabled") else 0
        )
        self.state["dataforseo_request_cap"] = (
            request_cap if self.state.get("dataforseo_enabled") else 0
        )
        counters["dataforseo_target_cap"] = (
            site_limit if self.state.get("dataforseo_enabled") else 0
        )
        counters["dataforseo_detail_cap"] = (
            detail_limit if self.state.get("dataforseo_enabled") else 0
        )
        counters["dataforseo_request_cap"] = (
            request_cap if self.state.get("dataforseo_enabled") else 0
        )
        self.state["counters"] = counters
        work_units = site_limit + detail_limit if self.state.get("dataforseo_enabled") else 0
        if not self.state.get("dataforseo_enabled"):
            self._event(
                "Search ranking correction skipped",
                detail="SEO was not enabled for this Smart Scan",
            )
            self.state["dataforseo_complete"] = True
            self._persist_state()
            return
        if self.state.get("dataforseo_complete"):
            self._event(
                "Search ranking correction reused",
                detail="This Smart Scan already committed its paid SEO pass",
            )
            self._advance_work(work_units, current="Search ranking correction reused")
            return
        raw_cohort = self.state.get("_dataforseo_cohort")
        cohort = (
            [dict(item) for item in raw_cohort if isinstance(item, dict)]
            if isinstance(raw_cohort, list)
            else []
        )
        from .paid_scan_plan import prior_detail_scope, seo_plan, seo_stage_receipts

        due_plan = self.state.get("_dataforseo_due_plan") is True
        if not isinstance(raw_cohort, list):
            def select():
                with LeadStore(database_path(), initialize=False, read_only=True) as store:
                    store.connection.execute("BEGIN")
                    return seo_plan(store, site_limit, detail_limit)

            plan = await self._await_seo_publication(select)
            cohort = plan["cohort"]
            due_plan = self.state["_dataforseo_due_plan"] = True
            detail_keys = set(plan["detail_scope"] or [])
            self.state["_dataforseo_detail_cohort"] = [
                item for item in cohort if item["seo_domain"] in detail_keys
            ]
        cohort = group_seo_cohort(cohort)
        if not due_plan:
            # Older in-progress runs retain their original cohort and receipt clock.
            launched = datetime.fromisoformat(str(self.state.get("started_at")
                                                  or datetime.now(UTC).isoformat()))
            with LeadStore(database_path(), initialize=False) as store:
                receipts = seo_stage_receipts(
                    store, [str(item["seo_domain"]) for item in cohort], now=launched,
                )
                historical_detail = prior_detail_scope(store, cohort, site_limit, now=launched)
            for item in cohort:
                root = str(item["seo_domain"])
                item["seo_due"] = (root, "bulk") not in receipts
                item["seo_detail_due"] = (root, "domain_overview") not in receipts
            if not self.state.get("_dataforseo_detail_cohort"):
                if historical_detail is not None:
                    selected = set(historical_detail[:detail_limit])
                    self.state["_dataforseo_detail_cohort"] = [
                        item for item in cohort if item["seo_domain"] in selected
                    ]
                elif not any(item["seo_due"] for item in cohort):
                    self.state["_dataforseo_detail_cohort"] = cohort[:detail_limit]
        self.state["_dataforseo_cohort"] = cohort
        by_domain = {item["seo_domain"]: item for item in cohort}
        self.state["_dataforseo_detail_cohort"] = [
            by_domain[item["seo_domain"]]
            for item in group_seo_cohort(self.state.get("_dataforseo_detail_cohort") or [])
            if item["seo_domain"] in by_domain
        ]
        self._persist_state()
        if due_plan:
            await self._flush_persistence()
        if not cohort:
            self._event(
                "Search ranking correction complete",
                detail="No live hydrated targets were eligible",
            )
            self.state["dataforseo_complete"] = True
            self._advance_work(work_units, current="Search ranking correction complete")
            return

        due_cohort = [item for item in cohort if bool(item.get("seo_due", True))]
        counters = dict(self.state.get("counters") or {})
        counters["dataforseo_cohort_targets"] = len(cohort)
        counters["dataforseo_due_targets"] = len(due_cohort)
        counters["dataforseo_fresh_reused"] = len(cohort) - len(due_cohort)
        counters.setdefault("dataforseo_locally_hydrated", 0)
        self.state["counters"] = counters
        self._persist_state()
        detail_pending = any(
            item.get("seo_detail_due", True)
            for item in self.state.get("_dataforseo_detail_cohort", [])
        )
        if not due_cohort and not detail_pending:
            self.state["dataforseo_complete"] = True
            self._event(
                "Search ranking correction reused",
                level="success",
                detail=(
                    f"{len(cohort):,} current top targets reuse SEO collected within "
                    "the last 90 days · no provider requests"
                ),
            )
            self._advance_work(work_units, current="Search ranking correction reused")
            return

        login = os.getenv(DATAFORSEO_LOGIN_SECRET, "")
        password = os.getenv(DATAFORSEO_PASSWORD_SECRET, "")
        if not login or not password:
            self._record_error(
                "DataForSEO credentials were removed after this Smart Scan was launched"
            )
            self._advance_work(work_units, current="Paid SEO correction unavailable")
            return

        candidates_by_domain = {
            str(item["seo_domain"]): item
            for item in cohort
        }
        scan_id = str(self.state.get("id") or "")
        with LeadStore(database_path()) as store:
            completed_bulk = store.dataforseo_run_completed_domains(
                scan_id,
                stage="bulk",
            )
        pending_bulk = [
            item for item in due_cohort if str(item["seo_domain"]) not in completed_bulk
        ]
        self._event(
            "Correcting the top target ranking with search visibility",
            detail=(
                f"{len(cohort):,} frozen top targets · {len(due_cohort):,} monthly due · "
                f"{len(pending_bulk):,} unresolved · "
                f"at most {request_cap} paid requests"
            ),
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            client = DataForSEOClient(login, password, client=http_client)
            for start in range(0, len(pending_bulk), DATAFORSEO_BULK_BATCH_LIMIT):
                await self._checkpoint()
                batch = pending_bulk[start : start + DATAFORSEO_BULK_BATCH_LIMIT]
                if not self._reserve_dataforseo_requests(1):
                    self._record_error("DataForSEO request ceiling reached before bulk completion")
                    break
                if due_plan:
                    await self._flush_persistence()
                try:
                    result = await client.bulk_traffic_estimation(
                        [str(item["seo_domain"]) for item in batch]
                    )
                    self._add_dataforseo_cost(result.cost_usd)
                    self._persist_state()
                    persisted, reprojected = await self._record_dataforseo_result(
                        result,
                        stage="bulk",
                        candidates_by_domain=candidates_by_domain,
                    )
                    counters = dict(self.state.get("counters") or {})
                    counters["dataforseo_bulk_returned"] = int(
                        counters.get("dataforseo_bulk_returned") or 0
                    ) + persisted
                    counters["dataforseo_reprojected"] = int(
                        counters.get("dataforseo_reprojected") or 0
                    ) + reprojected
                    self.state["counters"] = counters
                    self._event(
                        "Search visibility batch committed",
                        detail=(
                            f"{int(counters['dataforseo_bulk_returned']):,}/"
                            f"{len(due_cohort):,} due targets · actual spend "
                            f"${float(self.state['actual_dataforseo_cost_usd']):.4f}"
                        ),
                        replace_key="dataforseo-bulk-progress",
                    )
                    self._persist_state()
                except DataForSEOError as exc:
                    counters = dict(self.state.get("counters") or {})
                    counters["dataforseo_failures"] = int(
                        counters.get("dataforseo_failures") or 0
                    ) + len(batch)
                    self.state["counters"] = counters
                    self._record_error(f"DataForSEO bulk request: {exc}")
                    for item in batch:
                        self._action(
                            f"SEO failed · {item['canonical_domain']}",
                            detail=str(exc),
                            level="error",
                        )

            raw_detail = self.state.get("_dataforseo_detail_cohort")
            detail_cohort = (
                [dict(item) for item in raw_detail if isinstance(item, dict)]
                if isinstance(raw_detail, list)
                else []
            )
            if not detail_cohort:
                eligible = [
                    item for item in due_cohort if not due_plan or item["seo_detail_due"]
                ]
                eligible_roots = {str(item["seo_domain"]) for item in eligible}
                with LeadStore(database_path()) as store:
                    if due_plan:
                        provisional = store.dataforseo_ranking_candidates(
                            limit=site_limit,
                            only_roots={item["seo_domain"] for item in due_cohort},
                            bulk_selection=True,
                        )
                    else:
                        provisional = store.dataforseo_ranking_candidates(limit=site_limit)
                old_rank = {str(item["seo_domain"]): int(item["rank"]) for item in due_cohort}
                provisional = [
                    item for item in group_seo_cohort(provisional)
                    if str(item["seo_domain"]) in eligible_roots
                ]
                top_count = detail_limit * 3 // 5
                selected_domains = [str(item["seo_domain"]) for item in provisional[:top_count]]
                movers = sorted(
                    provisional,
                    key=lambda item: (
                        -abs(old_rank[str(item["seo_domain"])] - int(item["rank"])),
                        int(item["rank"]),
                        int(item["candidate_id"]),
                    ),
                )
                selected_domains.extend(str(item["seo_domain"]) for item in movers)
                if due_plan:
                    # Some frozen companies may briefly lack a current ranking after bulk.
                    selected_domains.extend(
                        item["seo_domain"] for item in cohort if item["seo_detail_due"]
                    )
                selected_domains = list(dict.fromkeys(selected_domains))[:detail_limit]
                detail_cohort = [candidates_by_domain[domain] for domain in selected_domains]
                self.state["_dataforseo_detail_cohort"] = detail_cohort
                self._persist_state()

            detail_cohort = [
                candidates_by_domain[str(item["seo_domain"])]
                for item in group_seo_cohort(detail_cohort)
                if str(item["seo_domain"]) in candidates_by_domain
            ]
            self.state["_dataforseo_detail_cohort"] = detail_cohort
            self._persist_state()
            with LeadStore(database_path()) as store:
                completed_detail = store.dataforseo_run_completed_domains(
                    scan_id,
                    stage="domain_overview",
                )
            pending_detail = [
                item
                for item in detail_cohort
                if str(item["seo_domain"]) not in completed_detail
                and bool(item.get("seo_detail_due", True))
            ]
            self._event(
                "Confirming high-impact search footprints",
                detail=(
                    f"{len(detail_cohort):,} selected from provisional leaders and rank movers · "
                    f"{len(pending_detail):,} unresolved"
                ),
            )
            for start in range(
                0,
                len(pending_detail),
                SMART_SCAN_DATAFORSEO_DETAIL_CONCURRENCY,
            ):
                await self._checkpoint()
                batch = pending_detail[
                    start : start + SMART_SCAN_DATAFORSEO_DETAIL_CONCURRENCY
                ]
                if not self._reserve_dataforseo_requests(len(batch)):
                    self._record_error(
                        "DataForSEO request ceiling reached before detail completion"
                    )
                    break
                if due_plan:
                    await self._flush_persistence()
                outcomes = await asyncio.gather(
                    *(
                        client.domain_rank_overview(str(item["seo_domain"]))
                        for item in batch
                    ),
                    return_exceptions=True,
                )
                await self._record_dataforseo_details(batch, outcomes, candidates_by_domain)
                counters = dict(self.state.get("counters") or {})
                self._event(
                    "Detailed search checks committed",
                    detail=(
                        f"{int(counters.get('dataforseo_detail_returned') or 0):,}/"
                        f"{len(detail_cohort):,} domains · actual spend "
                        f"${float(self.state['actual_dataforseo_cost_usd']):.4f}"
                    ),
                    replace_key="dataforseo-detail-progress",
                )
                self._persist_state()

        self.state["dataforseo_complete"] = True
        self._event(
            "Search ranking correction complete",
            level=(
                "warn"
                if int(dict(self.state.get("counters") or {}).get("dataforseo_failures") or 0)
                else "success"
            ),
            detail=(
                f"Actual provider spend ${float(self.state['actual_dataforseo_cost_usd']):.4f} · "
                "search visibility affects capacity only, never buyer intent"
            ),
        )
        self._advance_work(work_units, current="Search ranking correction complete")

    @measure_operation("ranking_preparation")
    async def _refresh_paid_scan_rankings(self) -> None:
        """Drain due local publication before a paid cohort is chosen; no providers."""
        from .sales_engine.ranking import refresh_rankings

        def batch():
            with LeadStore(database_path(), initialize=False) as store:
                result = refresh_rankings(store, limit=SMART_SCAN_RANKING_BATCH_SIZE)
                result["pending"] = store.connection.execute(
                    "SELECT COUNT(*) FROM engine_ranking_pending"
                ).fetchone()[0]
                return result

        previous_message = self.state.get("message", "")
        previous_current = dict(self.state.get("work") or {}).get("current", "")
        progress = {"refreshed": 0, "reused": 0, "failed": 0, "active": True}
        self.state["ranking_preparation"] = dict(progress)
        self.state["message"] = "Refreshing local company rankings"
        self._set_work(current="Refreshing local company rankings")
        self._event("Refreshing local company rankings", replace_key="ranking-preparation")
        self._persist_state()
        while True:
            await self._checkpoint()
            result = await self._await_seo_publication(batch)
            progress["refreshed"] += int(result.get("refreshed") or 0)
            progress["reused"] += int(result.get("reused") or 0)
            progress["failed"] += len(result.get("errors") or [])
            progress["pending"] = result["pending"]
            progress["backfill_complete"] = result.get("backfill_complete", True)
            self.state["ranking_preparation"] = dict(progress)
            if self.state.get("control_status", "running") == "running":
                self.state["message"] = "Refreshing local company rankings"
            self._event(
                "Refreshing local company rankings", replace_key="ranking-preparation",
                detail=(f"{progress['refreshed']:,} refreshed · {progress['reused']:,} reused · "
                        f"{progress['failed']:,} failed · {progress['pending']:,} queued"),
            )
            self._persist_state()
            if result.get("errors"):
                self._record_error("Some local company rankings could not be published")
            if not result.get("selected") and result.get("backfill_complete", True):
                break
        self.state["ranking_preparation"] = {**progress, "active": False}
        self.state["message"] = previous_message
        self._set_work(current=previous_current)
        self._persist_state()

    @measure_operation("paid_ads")
    async def _run_paid_ads(self) -> None:
        if self.state.get("paid_ads_complete") or not self.state.get("paid_ads_enabled"):
            return
        child_id = self.state.get("paid_ads_run_id")
        if child_id is None:
            await self._refresh_paid_scan_rankings()
            plan = await asyncio.to_thread(
                _smart_scan_ad_plan, int(self.state["paid_ads_company_limit"]),
                Decimal(str(self.state["paid_ads_max_usd"])),
            )
            if not plan["candidate_ids"]:
                self.state["paid_ads_complete"] = True
                self._event("Paid ads reused or unavailable",
                            detail="No due eligible selected companies")
                self._advance_work(int(self.state["paid_ads_company_limit"]),
                                   current="Paid ads checked")
                return
            quoted = ad_discovery_manager.create_quote(plan["request"])

            async def checkpoint(run_id: int) -> None:
                self.state["paid_ads_run_id"] = run_id
                self.state["paid_ads_candidate_ids"] = plan["candidate_ids"]
                self._persist_state()
                await self._flush_persistence()

            await ad_discovery_manager.start(
                AdDiscoveryRunStart(quote_id=quoted["quote"]["quote_id"], confirmed=True,
                                    confirmed_max_usd=Decimal(str(self.state["paid_ads_max_usd"]))),
                parent_checkpoint=checkpoint, seed_candidate_ids=plan["candidate_ids"],
            )
            child_id = self.state["paid_ads_run_id"]
        try:
            if ad_discovery_manager.run_id == child_id and ad_discovery_manager.running:
                task = ad_discovery_manager.task
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    self.last_runtime_tick = datetime.now(UTC)
                task.result()
        except asyncio.CancelledError:
            # Drain/abort the already-authorized child, never submit a replacement on resume.
            if ad_discovery_manager.run_id == child_id and ad_discovery_manager.running:
                await ad_discovery_manager.shutdown()
            raise
        with LeadStore(database_path(), initialize=False) as store:
            child = store.get_ad_discovery_run(int(child_id))
        if child and child["status"] in {"complete", "partial"}:
            def publish_ranking():
                from .sales_engine.ranking import refresh_candidate_rankings

                with LeadStore(database_path(), initialize=False) as store:
                    return refresh_candidate_rankings(
                        store, store.ad_observation_candidate_ids(int(child_id)),
                        reuse_current=True,
                    )
            await self._await_seo_publication(publish_ranking)
        self.state["paid_ads_complete"] = True
        self.state["actual_paid_ads_cost_usd"] = (
            float(Decimal(child["actual_cost_microusd"]) / 1_000_000) if child else 0.0
        )
        if not child or child["status"] != "complete":
            self._record_error(
                f"Paid ads run {child_id}: {child['status'] if child else 'missing'}; "
                "existing work retained, no automatic paid retry",
            )
        self._event("Paid ads research finished", detail=(
            f"Run {child_id} · actual ${float(self.state['actual_paid_ads_cost_usd']):.4f}"))
        self._advance_work(int(self.state["paid_ads_company_limit"]), current="Paid ads checked")
        self._persist_state()

    @measure_operation("microlink_hydration")
    async def _run_microlink_hydration(self) -> None:
        from .microlink_contacts_scan import recent_contact_companies
        from .microlink_hydration import FEATURES, run_hydration, select_cohort

        options = MicrolinkHydrationOptions.model_validate(
            self.state.get("microlink_hydration") or {})
        if not options.enabled or self.state.get("microlink_hydration_complete"):
            return
        if not os.getenv(MICROLINK_SECRET):
            raise ValueError("Configure Microlink in Settings before enabling hydration")
        if not self.state.get("microlink_hydration_run_id"):
            await self._refresh_paid_scan_rankings()
            def prepare():
                with LeadStore(database_path(), initialize=False) as store:
                    contact_only = options.contacts and not any(
                        getattr(options, name) for name in FEATURES if name != "contacts"
                    )
                    excluded = (
                        recent_contact_companies(store.connection, datetime.now(UTC))
                        if contact_only else ()
                    )
                    cohort = select_cohort(store, options.company_limit, exclude_companies=excluded)
                    run_id = store.start_run({"smart_scan_id": self.state["id"],
                                              "microlink_hydration": options.model_dump()})
                    return cohort, run_id
            cohort, run_id = await self._await_seo_publication(prepare)
            self.state.update(_microlink_cohort=cohort, microlink_hydration_run_id=run_id)
            self._persist_state()
            await self._flush_persistence()

        def progress(result):
            self.state["microlink_hydration_result"] = result
            self._event("Hydrating Microlink evidence", replace_key="microlink-progress",
                        detail=f"{result['completed']} companies · {result['requests']} requests")
            self._persist_state()

        result = await run_hydration(
            database_path(), int(self.state["microlink_hydration_run_id"]), options,
            os.environ[MICROLINK_SECRET], self.state.get("_microlink_cohort") or [],
            checkpoint=self._checkpoint, progress=progress,
        )
        self.state.update(microlink_hydration_result=result, microlink_hydration_complete=True)
        if result.get("failed"):
            self._record_error(f"Microlink hydration: {result['failed']} companies retain gaps")

        def finish():
            with LeadStore(database_path(), initialize=False) as store:
                store.finish_run(int(self.state["microlink_hydration_run_id"]),
                                 "partial" if result.get("failed") else "complete", result)
        await self._await_seo_publication(finish)
        await self._flush_persistence()
        await self.refresh_error_presentation(limit=1)
        await self._refresh_paid_scan_rankings()
        self._advance_work(1, current="Microlink hydration complete")
        self._persist_state()

    @measure_operation("people_hydration")
    async def _run_people_hydration(self) -> None:
        """Hydrate the frozen top due companies within the selected provider cap."""
        from .async_work import run_thread_owned

        due_units = int(
            dict(self.state.get("counters") or {}).get("people_hydration_due") or 0
        )
        if self.state.get("people_hydration_complete"):
            self._event(
                "People hydration already complete",
                detail="Durable scan state prevents duplicate work after resume",
            )
            return
        if not self.state.get("people_hydration_enabled"):
            self.state["people_hydration_complete"] = True
            self._event(
                "People hydration skipped",
                level="warn",
                detail="People hydration was not selected for this Smart Scan",
            )
            return
        manager = PeopleHydrationManager()
        company_limit = int(self.state.get("people_company_limit") or TREG_PEOPLE_MAX_COHORT_SIZE)
        cohort = self.state.get("_people_due_cohort")
        if cohort is None:
            def select():
                with LeadStore(database_path(), initialize=False, read_only=True) as store:
                    return store.treg_people_ranking_candidates(
                        limit=company_limit, due_only=True, include_target=False,
                    )

            cohort = await run_thread_owned(select)
            self.state["_people_due_cohort"] = cohort
            self._persist_state()
            await self._flush_persistence()
        result = await manager.run_scheduled(
            trigger="smart_scan",
            company_limit=company_limit,
            frozen_cohort=cohort,
            checkpoint=self._checkpoint,
        )
        counters = dict(self.state.get("counters") or {})
        counters.update(
            {
                "people_hydration_completed": int(
                    result.get("completed_companies") or 0
                ),
                "people_hydration_reused": int(result.get("reused_companies") or 0),
                "people_hydration_failed": int(result.get("failed_companies") or 0),
                "people_hydration_contacts": int(result.get("people_found") or 0),
                "people_hydration_quality_protected": int(
                    result.get("quality_protected_companies") or 0
                ),
            }
        )
        self.state.update(
            {
                "counters": counters,
                "people_hydration": result,
                "people_hydration_complete": result.get("status") in {
                    "complete",
                    "skipped",
                },
                "actual_people_hydration_cost_usd": float(
                    result.get("actual_cost_usd") or 0
                ),
            }
        )
        failed = int(result.get("failed_companies") or 0)
        if result.get("status") == "failed":
            self._record_error(
                "Treg people hydration stopped before the ranked cohort completed",
                event_message="People hydration failed",
            )
        elif failed:
            self._record_error(
                f"Treg people hydration had {failed} recoverable company failure(s)",
                event_message="People hydration completed with recoverable gaps",
            )
        self._event(
            "Decision-maker hydration complete",
            level="warn" if failed else "success",
            detail=(
                f"{int(result.get('people_found') or 0):,} accepted contacts · "
                f"{int(result.get('completed_companies') or 0):,} completed checks held "
                f"for {TREG_PEOPLE_REFRESH_DAYS} days · actual spend "
                f"${float(result.get('actual_cost_usd') or 0):.4f}"
            ),
        )
        self._advance_work(due_units, current="Decision-maker hydration complete")

    @measure_operation("ai_shortlist")
    async def _run_ai_shortlist(self) -> None:
        ai_site_limit = int(self.state.get("ai_site_limit") or 0)
        if not self.state.get("ai_enabled") or ai_site_limit <= 0:
            self._event("AI shortlist skipped",
                        detail="Company AI reviews were not selected for this Smart Scan")
            return
        if not os.getenv(OPENAI_SECRET):
            self._event("AI shortlist skipped", detail="OpenAI is not configured")
            self._advance_work(ai_site_limit, current="AI shortlist skipped")
            return
        from .async_work import run_thread_owned
        from .paid_scan_plan import ai_plan

        plan = self.state.get("_candidate_ai_plan")
        if plan is None:
            def select():
                with LeadStore(database_path(), initialize=False, read_only=True) as store:
                    return ai_plan(
                        store, ai_site_limit, model=os.getenv("BEINGS_LEADS_MODEL", "gpt-5-mini"),
                        web_limit=SMART_SCAN_CANDIDATE_AI_WEB_CALLS,
                    )

            plan = await run_thread_owned(select)
            self.state["_candidate_ai_plan"] = plan
        shortlist = plan["hosts"]
        self.state["ai_fresh_due"] = plan["due"]
        self.state["ai_reused"] = plan["reused"]
        self._persist_state()
        await self._flush_persistence()
        if not shortlist:
            self._event(
                "AI shortlist complete",
                detail=f"No new reviews due · {plan['reused']:,} recent or cached reviews skipped",
            )
            self._advance_work(ai_site_limit, current="AI shortlist complete")
            return
        self._event(
            "Reviewing the evidence-gap shortlist",
            detail=(
                f"{plan['reused']:,} recent or cached reviews skipped · "
                f"{len(shortlist):,} total candidates offered · at most "
                f"{ai_site_limit} fresh calls"
            ),
        )
        try:
            result = await self._wait_for_scan(
                ScanRequest(
                    sources=[],
                    only_hosts=shortlist,
                    max_hosts_per_source=len(shortlist),
                    max_network_inspections=len(shortlist),
                    adaptive_refresh=True,
                    reuse_core_only=True,
                    concurrency=20,
                    pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
                    english_only=True,
                    free_intelligence=False,
                    max_sitemaps_per_site=4,
                    max_common_crawl_lookups=0,
                    max_news_lookups=0,
                    max_frontier_scans=0,
                    agency_discovery=False,
                    ai=True,
                    ai_web=True,
                    min_ai_score=45,
                    max_ai_calls=ai_site_limit,
                    max_ai_web_calls=min(SMART_SCAN_CANDIDATE_AI_WEB_CALLS, ai_site_limit),
                    evergreen_discovery=False,
                ),
                [],
                len(shortlist),
            )
            run_id = result.get("run_id")
            if run_id:
                with LeadStore(database_path()) as store:
                    summary = dict(store.run_metadata(int(run_id)).get("summary") or {})
                counters = dict(self.state.get("counters") or {})
                counters["candidate_ai_reviewed"] = int(summary.get("ai_reviewed") or 0)
                counters["candidate_ai_fresh_calls"] = int(summary.get("ai_calls") or 0)
                self.state["counters"] = counters
            if str(result.get("status")) not in {"complete", "partial"}:
                self._record_error(f"AI shortlist: {result.get('message') or result.get('status')}")
        except Exception as exc:
            self._record_error(f"AI shortlist: {type(exc).__name__}: {exc}")
        self._advance_work(ai_site_limit, current="AI shortlist complete")

    @measure_operation("source_and_hydration_pipeline")
    async def _run_source_hydration(self) -> None:
        if self.state.get("source_pipeline_version") == 2:
            await self._run_parallel_discovery_hydration()
            return
        if self.state.get("source_hydration_complete"):
            return
        sources_pending = not self.state.get("source_phase_complete")
        if sources_pending:
            self.state["source_pipeline_version"] = 1
            if not self.state.get("foreground_sources_complete"):
                await self._checkpoint()
                self._set_phase("source_research", "Finding new sources for company discovery")
                try:
                    source_ai_reserved = await self._run_source_research()
                except Exception as exc:
                    source_ai_reserved = 0
                    self._record_error(f"Source research: {type(exc).__name__}: {exc}")
                self._advance_work(current="Source research checked")
                await self._checkpoint()
                self._set_phase("source_execution", "Running directory and web sources")
                try:
                    await self._run_accepted_sources(source_ai_reserved)
                except Exception as exc:
                    self._record_error(f"Accepted sources: {type(exc).__name__}: {exc}")
                try:
                    await self._run_free_source_methods()
                    await self._refresh_discovery_inventory_async()
                except Exception as exc:
                    self._record_error(f"Free source methods: {type(exc).__name__}: {exc}")
                try:
                    await self._run_rss_sources()
                    await self._refresh_discovery_inventory_async()
                except Exception as exc:
                    self._record_error(f"RSS discovery: {type(exc).__name__}: {exc}")
                self.state["foreground_sources_complete"] = True
                self._persist_state()
        else:
            self._event(
                "Completed source phase reused",
                detail="Continuing this scan from its saved hydration boundary",
            )
        await self._checkpoint()
        self._set_phase("base_hydration", "Hydrating the monthly-due candidate base")
        late_hydration_after = self.state.get("late_hydration_after")
        try:
            if late_hydration_after is not None:
                await self._run_base_hydration(after_candidate_id=int(late_hydration_after))
                self.state.pop("late_hydration_after", None)
                self._persist_state()
            else:
                await self._run_base_hydration()
        except Exception as exc:
            self._record_error(f"Base hydration: {type(exc).__name__}: {exc}")
        if sources_pending:
            await self._refresh_discovery_inventory_async()
            self.state["source_phase_complete"] = True
        boundary = int(self.state.get("candidate_boundary") or 0)
        latest = int(self.state.get("market_candidate_boundary") or 0)
        if late_hydration_after is None and latest > boundary:
            self.state["late_hydration_after"] = boundary
            self._persist_state()
            try:
                self._set_phase("base_hydration", "Hydrating companies saved by parallel sources")
                await self._run_base_hydration(after_candidate_id=boundary)
                self.state.pop("late_hydration_after", None)
            except Exception as exc:
                self._record_error(f"Late-source hydration: {type(exc).__name__}: {exc}")
        self.state["source_hydration_complete"] = True
        self._persist_state()

    async def _run_parallel_discovery_hydration(self) -> None:
        """Refresh the launch inventory while discovery prepares the next scan."""
        if self.state.get("foreground_sources_complete") and not all(
            self.state.get(key) for key in (
                "accepted_sources_complete", "public_graph_complete", "web_ct_complete",
            )
        ):
            # A previous process may have recorded a source-stage finish after a
            # writer collision. Reopen only the incomplete source step on resume.
            self.state["foreground_sources_complete"] = False
            self.state["source_hydration_complete"] = False
            self._persist_state()
        if self.state.get("source_hydration_complete"):
            return
        if self.state.get("candidate_boundary") is None:
            boundary = await asyncio.to_thread(self._read_candidate_boundary)
            self.state["candidate_boundary"] = boundary
            self.state["hydration_scope_after"] = 0
            self._persist_state()
            await self._flush_persistence()
            self._event(
                "Company refresh boundary frozen",
                detail=f"Existing candidate IDs through {boundary:,}; new IDs wait for next scan",
            )

        hydration_finished = asyncio.Event()

        async def discover() -> None:
            if not self.state.get("foreground_sources_complete"):
                if not self.state.get("source_research_complete"):
                    try:
                        reserved = await self._run_source_research()
                    except Exception as exc:
                        reserved = 0
                        self._record_error(f"Source research: {type(exc).__name__}: {exc}")
                    self.state["source_ai_reserved"] = reserved
                    self.state["source_research_complete"] = True
                    self._persist_state()
                if not self.state.get("accepted_sources_complete"):
                    try:
                        await self._run_accepted_sources(
                            int(self.state.get("source_ai_reserved") or 0)
                        )
                        self.state["accepted_sources_complete"] = True
                        self._persist_state()
                    except Exception as exc:
                        self._record_error(f"Accepted sources: {type(exc).__name__}: {exc}")
                if not self.state.get("public_graph_complete"):
                    for attempt in range(3):
                        try:
                            result = await self._run_free_source_method(
                                "public_graph", hydrate_companies=False,
                            )
                        except sqlite3.OperationalError as exc:
                            result = "database_locked" if "locked" in str(exc).lower() else "error"
                            self._record_error(
                                f"Public graph: {type(exc).__name__}: {exc}"
                            )
                        except Exception as exc:
                            result = "error"
                            self._record_error(
                                f"Public graph: {type(exc).__name__}: {exc}"
                            )
                        if result != "database_locked":
                            if result in {"complete", "partial", "failed"}:
                                self.state["public_graph_complete"] = True
                                self._persist_state()
                            break
                        if attempt < 2:
                            self._event("Public graph waiting for database writer")
                            if not hydration_finished.is_set():
                                await hydration_finished.wait()
                            else:
                                await asyncio.sleep(2 ** attempt)
                if not self.state.get("web_ct_complete"):
                    for attempt in range(3):
                        try:
                            await self._run_web_ct_discovery_only()
                            self.state["web_ct_complete"] = True
                            self._persist_state()
                            break
                        except sqlite3.OperationalError as exc:
                            self._record_error(f"Web & CT: {type(exc).__name__}: {exc}")
                            if "locked" not in str(exc).lower() or attempt == 2:
                                break
                            self._event("Web & CT waiting for database writer")
                            if not hydration_finished.is_set():
                                await hydration_finished.wait()
                            else:
                                await asyncio.sleep(2 ** attempt)
                        except Exception as exc:
                            self._record_error(f"Web & CT: {type(exc).__name__}: {exc}")
                            break
                rss_complete = True
                try:
                    await self._run_rss_sources()
                except Exception as exc:
                    rss_complete = False
                    self._record_error(f"RSS discovery: {type(exc).__name__}: {exc}")
                self.state["foreground_sources_complete"] = bool(
                    self.state.get("accepted_sources_complete")
                    and self.state.get("public_graph_complete")
                    and self.state.get("web_ct_complete")
                    and rss_complete
                )
                self._persist_state()

        async def hydrate() -> None:
            try:
                await self._run_base_hydration()
            except Exception as exc:
                self._record_error(f"Base hydration: {type(exc).__name__}: {exc}")
            finally:
                hydration_finished.set()

        self._set_phase("source_execution", "Finding new companies and refreshing profiles")
        tasks = [
            asyncio.create_task(discover()),
            asyncio.create_task(hydrate()),
        ]
        try:
            await asyncio.gather(*tasks)
            self.state["source_phase_complete"] = True
            await self._refresh_discovery_inventory_async()
            self.state["source_hydration_complete"] = bool(
                self.state.get("foreground_sources_complete")
                and dict(self.state.get("hydration_progress") or {}).get("stage") == "complete"
            )
            self._persist_state()
        finally:
            from .async_work import cancel_and_drain

            await cancel_and_drain(tasks)

    async def _run(self) -> None:
        try:
            await self._checkpoint()
            if statuses := self.state.get("parallel_sources"):
                for method in (RETIRED_LEGAL_DISCOVERY_METHOD, "news_reader"):
                    if statuses.get(method) in {"running", "interrupted"}:
                        statuses[method] = "cancelled"
            if self.state.get("reconnect_pending"):
                await self._wait_for_internet(reason="Smart Scan resumed while offline")
            # Older saved runs already in hydration finished source collection.
            # Preserve that boundary before planning overwrites the phase label.
            if not self.state.get("source_pipeline_version") and self.state.get("phase") in {
                "base_hydration", "seo_correction", "paid_ads", "people_hydration", "ai_shortlist"
            }:
                self.state["source_phase_complete"] = True
            self._set_phase("planning", "Mapping source and hydration gaps")
            self._advance_work(current="Backlog mapped")
            if self.state.get("due_hydration_enabled", True):
                await self._run_source_hydration()
            else:
                self._event(
                    "Due hydration skipped",
                    detail="Source discovery, company hydration and people hydration are off",
                )
            if MicrolinkHydrationOptions.model_validate(
                    self.state.get("microlink_hydration") or {}).enabled:
                await self._checkpoint()
                self._set_phase("microlink_hydration", "Hydrating selected Microlink evidence")
                try:
                    await self._run_microlink_hydration()
                except DesktopRankingUnavailable as exc:
                    self._record_error(f"Microlink hydration deferred: {exc}")
                except Exception as exc:
                    self._record_error(f"Microlink hydration: {type(exc).__name__}")
            if self.state.get("dataforseo_enabled"):
                await self._checkpoint()
                self._set_phase("seo_correction", "Correcting target order with search visibility")
                try:
                    await self._run_dataforseo_correction()
                except Exception as exc:
                    self._record_error(f"SEO correction: {type(exc).__name__}: {exc}")
            if self.state.get("paid_ads_enabled"):
                await self._checkpoint()
                self._set_phase("paid_ads", "Researching ads for the ranked company cohort")
                try:
                    await self._run_paid_ads()
                except Exception as exc:
                    self._record_error(f"Paid ads: {type(exc).__name__}: {exc}")
            if self.state.get("ai_enabled"):
                await self._checkpoint()
                self._set_phase("ai_shortlist", "Applying the bounded AI shortlist")
                try:
                    await self._run_ai_shortlist()
                except Exception as exc:
                    self._record_error(f"AI shortlist: {type(exc).__name__}: {exc}")
            if self.state.get("people_hydration_enabled"):
                await self._checkpoint()
                if self.state.get("paid_ads_enabled"):
                    await self._refresh_paid_scan_rankings()
                self._set_phase(
                    "people_hydration",
                    "Hydrating due decision-makers for the ranked cohort",
                )
                try:
                    await self._run_people_hydration()
                except Exception as exc:
                    self._record_error(f"People hydration: {type(exc).__name__}: {exc}")
            final_inventory = await asyncio.to_thread(
                _smart_scan_inventory_for_policy, self.state.get("max_company_failure_rate")
            )
            error_total = int(self.state.get("error_total") or 0)
            status = "partial" if error_total else "complete"
            finished_at = datetime.now(UTC).isoformat()
            work = dict(self.state.get("work") or {})
            self._set_work(
                completed=int(work.get("total") or 0),
                current="Smart Scan complete",
            )
            self.state.update(
                {
                    "status": status,
                    "control_status": "complete",
                    "progress_percent": 100.0,
                    "phase": "complete",
                    "phase_index": len(SMART_SCAN_PHASES),
                    "message": (
                        "Smart Scan completed with recoverable gaps"
                        if error_total
                        else "Smart Scan complete"
                    ),
                    "inventory": final_inventory,
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    "child_scan_job_id": None,
                }
            )
            self._event(
                str(self.state["message"]),
                level="warn" if error_total else "success",
                detail=f"{int(final_inventory.get('due_candidates') or 0):,} records remain due",
            )
            self._persist_state()
        except asyncio.CancelledError:
            timestamp = datetime.now(UTC).isoformat()
            control_status = str(self.state.get("control_status") or "running")
            if control_status == "paused":
                self.state.update(
                    {
                        "status": "paused",
                        "message": "Smart Scan paused; completed work is preserved",
                        "updated_at": timestamp,
                    }
                )
            elif self.shutting_down_for_restart and control_status == "running":
                if control_status == "running":
                    self.state.update(
                        {
                            "status": "restarting",
                            "message": "Smart Scan will resume after app restart",
                            "updated_at": timestamp,
                        }
                    )
                    self._event(str(self.state["message"]), level="warn")
            else:
                already_stopped = str(self.state.get("status")) == "stopped"
                self.state.update(
                    {
                        "status": "stopped",
                        "control_status": "stopped",
                        "phase": "stopped",
                        "message": "Smart Scan stopped; completed work is preserved",
                        "finished_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
                if not already_stopped:
                    self._event(str(self.state["message"]), level="warn")
            self._persist_state()
            raise
        except Exception as exc:
            logger.exception("smart scan crashed")
            self._record_error(f"{type(exc).__name__}: {exc}")
            finished_at = datetime.now(UTC).isoformat()
            self.state.update(
                {
                    "status": "failed",
                    "phase": "failed",
                    "message": "Smart Scan failed before all phases completed",
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            self._persist_state()

        finally:
            await self._flush_persistence()

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.shutting_down_for_restart = True
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self._stop_cleanup is not None:
            await self._stop_cleanup
        await self._flush_persistence()


smart_scan_manager = SmartScanManager()


class PeopleHydrationManager:
    """Run a bounded Treg people refresh from an explicit quote or scan orchestration."""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.quotes: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] = {
            "status": "idle",
            "message": "People hydration is ready",
        }

    running = ScanManager.running

    @staticmethod
    def _public_quote(quote: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in quote.items()
            if key not in {"cohort", "maximum_decimal", "used"}
        }

    def _build_quote(
        self,
        request: PeopleHydrationEstimateRequest,
        *,
        candidate_ids: set[int] | None = None,
        frozen_cohort: list[dict[str, Any]] | None = None,
        trigger: str = "operator_quote",
        remember: bool = True,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        if remember:
            self.quotes = {
                quote_id: quote
                for quote_id, quote in self.quotes.items()
                if datetime.fromisoformat(str(quote["expires_at"])) > now
                and not quote.get("used")
            }
        if frozen_cohort is None:
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                cohort = store.treg_people_ranking_candidates(
                    limit=request.company_limit, as_of=now, due_only=True,
                    candidate_ids=candidate_ids, include_target=False,
                )
        else:
            cohort = frozen_cohort[:request.company_limit]
        due = [dict(item) for item in cohort if bool(item.get("people_due", True))]
        estimate = estimate_people_hydration(
            company_count=len(due),
            people_limit=request.people_limit,
            email_limit=request.email_limit,
            email_max_cost_usd=request.email_max_cost_usd,
        )
        quote_id = f"people_{uuid.uuid4().hex}"
        expires_at = now + timedelta(minutes=PEOPLE_HYDRATION_QUOTE_TTL_MINUTES)
        maximum_decimal = Decimal(str(estimate["maximum_usd"]))
        quote: dict[str, Any] = {
            "quote_id": quote_id,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "configured": resolve_treg_credentials() is not None,
            "trigger": trigger,
            "ranked_cohort_count": len(cohort),
            "cohort_count": len(cohort),
            "fresh_companies": len(cohort) - len(due),
            "due_companies": len(due),
            "refresh_days": TREG_PEOPLE_REFRESH_DAYS,
            "company_limit": request.company_limit,
            "people_limit": request.people_limit,
            "email_limit": request.email_limit,
            "email_max_cost_usd": float(request.email_max_cost_usd),
            **estimate,
            "cost_note": (
                "Maximum assumes every requested people row is returned. "
                "Emails are requested only from a company button. "
                "Fewer results and 90-day-held companies can lower actual spend; "
                f"only results with at least {TREG_PEOPLE_MIN_GOOD_CONTACTS} accepted contacts "
                "earn the hold."
            ),
            "cohort": due,
            "maximum_decimal": maximum_decimal,
            "used": False,
        }
        if remember:
            self.quotes[quote_id] = quote
            while len(self.quotes) > 20:
                self.quotes.pop(next(iter(self.quotes)))
        return quote

    def estimate(self, request: PeopleHydrationEstimateRequest) -> dict[str, Any]:
        quote = self._build_quote(request)
        return self._public_quote(quote)

    def _initialize_run_state(
        self,
        quote: Mapping[str, Any],
        *,
        trigger: str,
    ) -> None:
        now = datetime.now(UTC)
        self.state = {
            "id": f"people_run_{uuid.uuid4().hex[:16]}",
            "status": "running",
            "message": "Hydrating ranked-company people",
            "trigger": trigger,
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "finished_at": None,
            "quote_id": quote["quote_id"],
            "company_count": len(quote["cohort"]),
            "processed_companies": 0,
            "completed_companies": 0,
            "reused_companies": 0,
            "failed_companies": 0,
            "people_found": 0,
            "verified_emails": 0,
            "quality_protected_companies": 0,
            "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
            "actual_cost_usd": 0.0,
            "confirmed_max_usd": float(quote["maximum_decimal"]),
            "current_company": "",
            "errors": [],
        }

    async def start(self, request: PeopleHydrationRunStart) -> dict[str, Any]:
        if self.running:
            raise HTTPException(status_code=409, detail="People hydration is already running")
        quote = self.quotes.get(request.quote_id)
        if quote is None or quote.get("used"):
            raise HTTPException(status_code=409, detail="People hydration quote is unavailable")
        if datetime.fromisoformat(str(quote["expires_at"])) <= datetime.now(UTC):
            self.quotes.pop(request.quote_id, None)
            raise HTTPException(status_code=409, detail="People hydration quote expired")
        if not request.confirmed:
            raise HTTPException(
                status_code=422,
                detail="Confirm the people hydration spend ceiling",
            )
        if request.confirmed_max_usd != quote["maximum_decimal"]:
            raise HTTPException(
                status_code=409,
                detail="Confirmed maximum does not match the current people hydration quote",
            )
        if resolve_treg_credentials() is None:
            raise HTTPException(status_code=422, detail="Treg is not configured")
        quote["used"] = True
        self._initialize_run_state(quote, trigger="operator_quote")
        self.task = asyncio.create_task(self._run(dict(quote)))
        return dict(self.state)

    async def run_scheduled(
        self,
        *,
        trigger: Literal["smart_scan", "manual_scan"],
        company_limit: int = TREG_PEOPLE_MAX_COHORT_SIZE,
        candidate_ids: set[int] | None = None,
        frozen_cohort: list[dict[str, Any]] | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Run the quote-bounded defaults as an authorized phase of a user-started scan."""

        quote = self._build_quote(
            PeopleHydrationEstimateRequest(company_limit=company_limit),
            candidate_ids=candidate_ids,
            frozen_cohort=frozen_cohort,
            trigger=trigger,
            remember=False,
        )
        now = datetime.now(UTC).isoformat()
        if not quote["configured"]:
            self.state = {
                "status": "skipped",
                "message": "Treg is not configured; people hydration was skipped",
                "trigger": trigger,
                "company_count": len(quote["cohort"]),
                "processed_companies": 0,
                "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
                "confirmed_max_usd": float(quote["maximum_decimal"]),
                "actual_cost_usd": 0.0,
                "finished_at": now,
            }
            return dict(self.state)
        self._initialize_run_state(quote, trigger=trigger)
        if not quote["cohort"]:
            self.state.update(
                status="complete",
                message="No ranked companies are due for people hydration",
                progress_percent=100.0,
                finished_at=now,
                updated_at=now,
            )
            return dict(self.state)
        await self._run(dict(quote), checkpoint=checkpoint)
        return dict(self.state)

    async def _hydrate_company(
        self,
        client: TregPeopleClient,
        item: Mapping[str, Any],
        quote: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = int(item["candidate_id"])
        domain = str(item["canonical_domain"])
        people_limit = int(quote["people_limit"])
        email_limit = 0  # Email lookup is exclusively a company-button action.
        execution_id: int | None = None
        charged_microusd = 0
        request_count = 0
        byte_count = 0
        try:
            from .email_lookup import recover_pending_people

            if not await recover_pending_people(database_path(), candidate_id, client):
                return {"status": "failed", "domain": domain, "cost_microusd": 0,
                        "cost_unknown": True,
                        "error": "Existing people lookup awaits settlement; no new request."}
            with LeadStore(database_path()) as store:
                claim = store.claim_scanner_work(
                    [candidate_id],
                    scanner_id=TREG_PEOPLE_SCANNER_ID,
                    scanner_version=TREG_PEOPLE_SCANNER_VERSION,
                    extractor_version=TREG_PEOPLE_EXTRACTOR_VERSION,
                    scope_key=f"ranked_top_{quote['company_limit']}:rank:{item['rank']}",
                    config={
                        "people_limit": people_limit,
                        "email_limit": email_limit,
                        "email_max_cost_usd": str(quote["email_max_cost_usd"]),
                        "role_policy": "beings-buyer-role-v1",
                    },
                    maximum=1,
                )
            if candidate_id not in claim["claimed"]:
                return {"status": "reused", "domain": domain, "cost_microusd": 0}
            execution_id = int(claim["claimed"][candidate_id])
            economic_limit = max(1, min(3, people_limit // 2))
            champion_limit = people_limit - economic_limit
            provider_people = []
            call_ids: list[str] = []
            response_hashes: list[str] = []
            for lane, titles, lane_limit in (
                ("economic", ECONOMIC_TITLES, economic_limit),
                ("champion", CHAMPION_TITLES, champion_limit),
            ):
                result = await client.search_people(
                    domain=domain,
                    titles=titles,
                    limit=lane_limit,
                    idempotency_key=(
                        f"{self.state['id']}:{execution_id}:people-search:{lane}"
                    ),
                )
                request_count += 1
                byte_count += result.response_bytes
                charged_microusd += result.charged_microusd
                provider_people.extend(result.people)
                call_ids.append(result.call_id)
                response_hashes.append(result.response_hash)
            people = list(
                select_people(
                    provider_people,
                    company_archetype=str(item.get("archetype") or ""),
                    limit=people_limit,
                )
            )
            email_attempts = 0
            projection = project_people_result(
                company_domain=domain,
                people=people,
                search_call_ids=call_ids,
                response_hashes=response_hashes,
                attempted_searches=2,
                attempted_emails=email_attempts,
            )
            status = "success" if projection.observations else "success_empty"
            with LeadStore(database_path()) as store:
                store.complete_scanner_execution(
                    execution_id,
                    ScannerExecutionSummary(
                        execution_id=execution_id,
                        candidate_id=candidate_id,
                        company_key=domain,
                        scanner_id=TREG_PEOPLE_SCANNER_ID,
                        scanner_version=TREG_PEOPLE_SCANNER_VERSION,
                        extractor_version=TREG_PEOPLE_EXTRACTOR_VERSION,
                        status=status,
                        request_count=request_count,
                        byte_count=byte_count,
                        cost_microusd=charged_microusd,
                        observation_count=len(projection.observations),
                        metadata={
                            "provider": "Treg",
                            "search_provider": "IcyPeas",
                            "provider_payload_retained": False,
                        },
                    ),
                    coverage_manifests=projection.coverage_manifests,
                    artifacts=projection.artifacts,
                    observations=projection.observations,
                    next_due_at=datetime.now(UTC)
                    + timedelta(days=TREG_PEOPLE_REFRESH_DAYS),
                )
            return {
                "status": "completed",
                "domain": domain,
                "cost_microusd": charged_microusd,
                "people": len(people),
                "verified_emails": sum(person.email_status == "found" for person in people),
                "quality_protected": len(people) >= TREG_PEOPLE_MIN_GOOD_CONTACTS,
            }
        except TregPeopleError as exc:
            message = str(exc)[:500]
            if execution_id is not None:
                failure_status = (
                    "rate_limited"
                    if "rate limited" in message.lower()
                    else "transient_error"
                )
                with LeadStore(database_path()) as store:
                    if isinstance(exc, TregUnsettledError):
                        from .email_lookup import preserve_unsettled_call

                        preserve_unsettled_call(store, domain, "people", exc)
                        store.connection.commit()
                    store.fail_scanner_execution(
                        execution_id,
                        status=failure_status,
                        error=message,
                        next_due_at=(datetime.now(UTC) + timedelta(days=TREG_PEOPLE_REFRESH_DAYS)
                                     if isinstance(exc, TregUnsettledError) else None),
                    )
            return {
                "status": "failed",
                "domain": domain,
                "cost_microusd": charged_microusd,
                "cost_unknown": isinstance(exc, TregUnsettledError),
                "error": message,
            }
        except Exception as exc:
            logger.error("people hydration company failed type=%s", type(exc).__name__)
            message = "People hydration could not complete this company"
            if execution_id is not None:
                with suppress(Exception), LeadStore(database_path()) as store:
                    store.fail_scanner_execution(
                        execution_id,
                        status="transient_error",
                        error=message,
                    )
            return {
                "status": "failed",
                "domain": domain,
                "cost_microusd": charged_microusd,
                "error": message,
            }

    async def _run(
        self,
        quote: Mapping[str, Any],
        *,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        credentials = resolve_treg_credentials()
        if credentials is None:
            self.state.update(
                status="failed",
                message="Treg configuration was removed before the run started",
                finished_at=datetime.now(UTC).isoformat(),
            )
            return
        cohort = [dict(item) for item in quote["cohort"]]
        maximum_microusd = int(
            (Decimal(str(quote["maximum_decimal"])) * Decimal("1000000")).to_integral_value()
        )
        actual_microusd = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(45.0, connect=10.0),
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                client = TregPeopleClient(credentials, client=http_client)
                for start in range(0, len(cohort), 5):
                    if checkpoint is not None:
                        await checkpoint()
                    batch = cohort[start : start + 5]
                    self.state["current_company"] = str(batch[0]["canonical_domain"])
                    outcomes = await asyncio.gather(
                        *(self._hydrate_company(client, item, quote) for item in batch)
                    )
                    for outcome in outcomes:
                        actual_microusd += int(outcome.get("cost_microusd") or 0)
                        if outcome.get("cost_unknown"):
                            self.state["cost_unknown"] = True
                        self.state["processed_companies"] += 1
                        if outcome["status"] == "completed":
                            self.state["completed_companies"] += 1
                            self.state["people_found"] += int(outcome.get("people") or 0)
                            self.state["verified_emails"] += int(
                                outcome.get("verified_emails") or 0
                            )
                            self.state["quality_protected_companies"] += int(
                                bool(outcome.get("quality_protected"))
                            )
                        elif outcome["status"] == "reused":
                            self.state["reused_companies"] += 1
                        else:
                            self.state["failed_companies"] += 1
                            errors = list(self.state.get("errors") or [])
                            errors.append(
                                {
                                    "company": str(outcome.get("domain") or "")[:253],
                                    "message": str(outcome.get("error") or "Failed")[:500],
                                }
                            )
                            self.state["errors"] = errors[-20:]
                    if actual_microusd > maximum_microusd:
                        raise RuntimeError("Treg reported spend above the confirmed maximum")
                    self.state["actual_cost_usd"] = (
                        None if self.state.get("cost_unknown") else actual_microusd / 1_000_000
                    )
                    self.state["updated_at"] = datetime.now(UTC).isoformat()
                    self.state["progress_percent"] = round(
                        100
                        * int(self.state["processed_companies"])
                        / max(1, int(self.state["company_count"])),
                        1,
                    )
            finished = datetime.now(UTC).isoformat()
            self.state.update(
                status="complete",
                message=(
                    "People hydration completed with some company-level failures"
                    if self.state["failed_companies"]
                    else "People hydration complete"
                ),
                current_company="",
                finished_at=finished,
                updated_at=finished,
                progress_percent=100.0,
            )
        except asyncio.CancelledError:
            finished = datetime.now(UTC).isoformat()
            self.state.update(
                status="cancelled",
                message="People hydration stopped during app shutdown",
                current_company="",
                finished_at=finished,
                updated_at=finished,
            )
            raise
        except Exception as exc:
            logger.error("people hydration run failed type=%s", type(exc).__name__)
            finished = datetime.now(UTC).isoformat()
            self.state.update(
                status="failed",
                message="People hydration stopped before completion",
                current_company="",
                finished_at=finished,
                updated_at=finished,
            )

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


people_hydration_manager = PeopleHydrationManager()


async def _run_people_hydration_for_manual_scan(run_id: int) -> dict[str, Any]:
    """Hydrate due top-cohort companies that received a snapshot in one manual scan."""

    if run_id <= 0:
        return {
            "status": "complete",
            "message": "No completed company snapshots were available for people hydration",
            "company_count": 0,
            "processed_companies": 0,
            "actual_cost_usd": 0.0,
        }
    with LeadStore(database_path()) as store:
        touched_rows = store.connection.execute(
            """
            SELECT DISTINCT COALESCE(ti.canonical_domain, c.hostname) AS company_key
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE s.run_id=?
            """,
            (int(run_id),),
        ).fetchall()
        touched_keys = {
            store._normalized_company_key(str(row["company_key"] or ""))
            for row in touched_rows
            if row["company_key"]
        }
        cohort = store.treg_people_ranking_candidates(
            limit=TREG_PEOPLE_MAX_COHORT_SIZE, include_target=False,
        )
    candidate_ids = {
        int(item["candidate_id"])
        for item in cohort
        if str(item["canonical_domain"]) in touched_keys
    }
    manager = PeopleHydrationManager()
    result = await manager.run_scheduled(
        trigger="manual_scan",
        candidate_ids=candidate_ids,
    )
    return {
        **result,
        "touched_companies": len(touched_keys),
        "ranked_touched_companies": len(candidate_ids),
    }


def _reject_new_work_during_smart_scan(*, allow_cleanup: bool = False) -> None:
    if _company_purge_active():
        raise HTTPException(409, "Wait for the company data purge to finish")
    if microlink_scan.active or microlink_contacts.active:
        raise HTTPException(409, "Wait for the active Microlink scan to finish")
    if allow_cleanup and getattr(smart_scan_manager, "cleaning_up", False):
        return
    if bool(
        getattr(
            smart_scan_manager,
            "active",
            getattr(smart_scan_manager, "running", False),
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Smart Scan is active; pause does not release its workflow ownership",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    with ExitStack() as ranking_listener:
        async with _service_lifespan(ranking_listener):
            yield


@asynccontextmanager
async def _service_lifespan(ranking_listener):
    from .desktop_ranking.server import configured_socket, serve_ranking
    from .sales_engine.maintenance import maintain_compiler_receipts, maintain_projections
    from .sales_engine.maintenance import mission_write_lock as ranking_setup_lock

    rate_token = DATABASE.set(database_path())
    if database_path() == DEFAULT_DB.resolve():
        from .starter_inventory import initialize_starter_inventory

        initialize_starter_inventory(database_path())
    sync_registry()
    with LeadStore(database_path()) as store:
        store.connection.execute(
            "UPDATE buyer_collection_quotes SET status='interrupted' "
            "WHERE status IN ('running','cancel_requested')")
        store.connection.commit()
        recovered_scouts = store.recover_running_scout_runs()
        recovered_ad_runs = store.recover_interrupted_ad_discovery_runs()
        retired_legal_runs = store.cancel_unfinished_source_execution_method(
            RETIRED_LEGAL_DISCOVERY_METHOD, reason="Legal filings collection retired",
        )
        retired_news_runs = store.cancel_unfinished_source_execution_method(
            "news_reader", reason="News reader collection retired",
        )
        recovered_source_runs = store.recover_running_source_execution_runs()
    if recovered_scouts:
        logger.warning("recovered stale source scout runs=%d", recovered_scouts)
    if recovered_ad_runs:
        logger.warning("parked interrupted paid-ad runs=%d without resuming", recovered_ad_runs)
    if retired_legal_runs:
        logger.info("cancelled unfinished Legal filings executions=%d", retired_legal_runs)
    if retired_news_runs:
        logger.info("cancelled unfinished News reader executions=%d", retired_news_runs)
    if recovered_source_runs:
        logger.warning("requeued interrupted public-source runs=%d", recovered_source_runs)
    ranking_socket = configured_socket(database_path())
    if ranking_socket is not None:
        ranking_listener.enter_context(serve_ranking(
            database_path(), ranking_socket,
            lambda: smart_scan_manager.active or ranking_setup_lock.locked(),
        ))
    source_scout_manager.start_scheduler()
    queued_source_runs = discovery_method_manager.resume_queued()
    if queued_source_runs:
        logger.info("resumed queued accepted-source runs=%d", queued_source_runs)
    recover_smart_scan = getattr(smart_scan_manager, "recover", None)
    if recover_smart_scan is not None and await recover_smart_scan():
        logger.info("resumed durable Smart Scan after app restart")
    recovery_task = asyncio.create_task(scan_manager.recover_stale_runs())
    if ranking_socket is None:
        projection_task = asyncio.create_task(maintain_projections(
            lambda: database_path(), should_defer=lambda: smart_scan_manager.active,
        ))
    else:
        projection_task = asyncio.create_task(maintain_compiler_receipts(database_path()))
    async def expire_diagnostics():
        from .error_logging import prune_runtime_logs

        def prune():
            with LeadStore(database_path()) as store:
                store.prune_error_logs()
            prune_runtime_logs(database_path().parent)

        while True:
            try:
                await asyncio.to_thread(prune)
            except Exception:
                logger.exception("diagnostic retention cleanup failed")
            await asyncio.sleep(3600)

    diagnostic_task = asyncio.create_task(expire_diagnostics())
    yield
    diagnostic_task.cancel()
    with suppress(asyncio.CancelledError):
        await diagnostic_task
    projection_task.cancel()
    with suppress(asyncio.CancelledError):
        await projection_task
    recovery_task.cancel()
    with suppress(asyncio.CancelledError):
        await recovery_task
    await people_hydration_manager.shutdown()
    await smart_scan_inventory_check.shutdown()
    await smart_scan_manager.shutdown()
    await microlink_scan.shutdown()
    await microlink_contacts.shutdown()
    await asyncio.to_thread(CENSUS.close)
    await ad_discovery_manager.shutdown()
    await discovery_method_manager.shutdown()
    scan_shutdown = getattr(scan_manager, "shutdown", None)
    if scan_shutdown is not None:
        await scan_shutdown()
    await source_scout_manager.stop_scheduler()
    from .page_compute import shutdown as close_page_compute
    from .scan_persistence import WRITER

    await asyncio.to_thread(close_page_compute)
    await asyncio.to_thread(WRITER.close)
    DATABASE.reset(rate_token)


def _has_active_user_mission() -> bool:
    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        return store.connection.execute(
            "SELECT 1 FROM engine_missions WHERE archived=0 AND id<>'beings' LIMIT 1"
        ).fetchone() is not None


class NoStoreAPIMiddleware:
    """Set cache headers without BaseHTTPMiddleware task/cancellation side effects."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                supplied = {
                    name.lower(): value for name, value in message.get("headers", [])
                }
                # Only explicitly versioned, private Sales reads opt into conditional
                # caching. Settings/secrets and every mutation retain no-store.
                if (
                    scope.get("method") in {"GET", "HEAD"}
                    and scope["path"].startswith("/api/sales-engine/")
                    and message.get("status") in {200, 304}
                    and supplied.get(b"etag")
                    and supplied.get(b"cache-control", b"").startswith(b"private")
                ):
                    await send(message)
                    return
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {b"cache-control", b"pragma"}
                ]
                message["headers"] = [
                    *headers,
                    (b"cache-control", b"no-store"),
                    (b"pragma", b"no-cache"),
                ]
            await send(message)

        from .release_mode import requires_user_mission

        public_install = requires_user_mission()
        if public_install or scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
            from .local_access import require_local

            try:
                require_local(Request(scope))
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                    scope, receive, send_no_store
                )
                return
        path = scope["path"]
        mission_api = "/api/sales-engine/missions"
        if public_install and not (
            path.startswith("/api/settings/") or path == mission_api
            or path.startswith(mission_api + "/")
        ):
            legacy_list = path in {"/api/sales-engine/overview", "/api/sales-engine/pipeline"}
            legacy_summary = path.startswith("/api/sales-engine/companies/") and path.endswith(
                "/summary"
            )
            if (legacy_list or legacy_summary) and Request(scope).query_params.get(
                "mission_id", "beings"
            ) == "beings":
                await JSONResponse(
                    {"detail": "Select a mission to continue."}, status_code=422,
                )(scope, receive, send_no_store)
                return
            if not await asyncio.to_thread(_has_active_user_mission):
                await JSONResponse(
                    {"detail": "Create a mission to get started."}, status_code=409,
                )(scope, receive, send_no_store)
                return
        with rate_limit_scope(database_path()):
            await self.app(scope, receive, send_no_store)


app = FastAPI(title="Beings Growth", version="0.2.0", lifespan=lifespan)
app.add_middleware(NoStoreAPIMiddleware)
app.include_router(create_sales_engine_router(lambda: database_path()))


@app.exception_handler(DesktopRankingUnavailable)
async def desktop_ranking_unavailable(_: Request, error: DesktopRankingUnavailable):
    return JSONResponse({"detail": str(error)}, status_code=503)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError) -> Response:
    if request.url.path.startswith("/api/settings/"):
        return JSONResponse(
            {"detail": "Check the credential fields and try again."}, status_code=422,
        )
    return await request_validation_exception_handler(request, error)


company_purge_task: asyncio.Task | None = None
company_purge_job: dict | None = None
company_purge_database: Path | None = None
company_purge_cancellation: PurgeCancellation | None = None
_company_purge_preview_lock = Lock()


def _company_purge_active():
    return company_purge_task is not None and not company_purge_task.done()


def _other_scan_active():
    return (_company_purge_active() or smart_scan_manager.active or scan_manager.running
            or discovery_method_manager.running or ad_discovery_manager.running)


microlink_scan = MicrolinkScan(
    lambda: database_path(),
    lambda: os.getenv(MICROLINK_SECRET, "").strip(),
    lambda: _other_scan_active() or microlink_contacts.active,
)
microlink_contacts = MicrolinkContactScan(
    lambda: database_path(),
    lambda: os.getenv(MICROLINK_SECRET, "").strip(),
    lambda: _other_scan_active() or microlink_scan.active,
)
app.include_router(microlink_scan.router())
app.include_router(microlink_contacts.router())


@app.get("/api/settings/company-purge")
def company_purge_settings():
    return purge_settings(database_path())


@app.put("/api/settings/company-purge/endings")
def company_purge_endings(request: ProtectedEndings):
    try:
        return save_protected_endings(database_path(), request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _company_purge_preview_events(database: Path, criteria: PurgeCriteria):
    loop = asyncio.get_running_loop()
    updates = asyncio.Queue()
    stopped = Event()

    def enqueue(event):
        # Keep at most one pending progress update during network backpressure.
        if event["type"] != "progress" or event["processed"] == 0 or updates.empty():
            updates.put_nowait(event)

    def publish(event):
        if not stopped.is_set():
            loop.call_soon_threadsafe(enqueue, event)

    def progress(phase, processed, total):
        publish({"type": "progress", "phase": phase, "processed": processed, "total": total})

    def run():
        if not _company_purge_preview_lock.acquire(blocking=False):
            publish({"type": "error", "message": "Another purge preview is running."})
            return
        try:
            result = preview_purge(
                database, criteria, progress=progress, cancelled=stopped.is_set,
                matches=lambda summary: publish({"type": "preview", "result": summary}),
            )
        except ValueError as exc:
            publish({"type": "error", "message": str(exc)})
        except Exception:
            if not stopped.is_set():
                logger.exception("Company purge preview failed")
                publish({"type": "error", "message": "The purge preview could not finish."})
        else:
            publish({"type": "complete", "result": result})
        finally:
            _company_purge_preview_lock.release()

    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        while True:
            try:
                event = await asyncio.wait_for(updates.get(), timeout=10)
            except TimeoutError:
                yield "\n"  # Keep the stream open during a long SQLite query.
                continue
            yield json.dumps(event) + "\n"
            if event["type"] in {"complete", "error"}:
                break
    finally:
        stopped.set()
        await asyncio.shield(worker)


@app.post("/api/settings/company-purge/preview")
def company_purge_preview(request: PurgeCriteria, stream: bool = False):
    database = database_path()
    if stream:
        return StreamingResponse(
            _company_purge_preview_events(database, request),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )
    try:
        return preview_purge(database, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _start_company_purge(request: PurgeConfirmation | None):
    global company_purge_task, company_purge_job, company_purge_database
    global company_purge_cancellation
    if (_other_scan_active() or source_scout_manager.running or people_hydration_manager.running
            or microlink_scan.active or microlink_contacts.active):
        raise HTTPException(409, "Finish active scans and purges before changing database storage.")
    database = database_path()
    cancellation = PurgeCancellation() if request is not None else None
    job = {
        "id": uuid.uuid4().hex, "status": "running", "progress": None,
        "kind": "purge" if request is not None else "reclaim",
        "phase": "deleting" if request is not None else "reclaiming",
    }
    if request is not None:
        job["criteria"] = request.job_criteria()

    def progress(processed, total):
        job["progress"] = {"processed": processed, "total": total}

    async def run():
        result = {}
        if request is not None:
            try:
                result = await asyncio.to_thread(
                    confirm_purge, database, request, progress=progress, cancellation=cancellation,
                )
            except PurgeCancelled:
                job.update(status="cancelled")
                return dict(job)
            except ValueError as exc:
                job.update(status="failed", error=str(exc))
                return dict(job)
            except Exception:
                logger.exception("Company purge transaction failed")
                job.update(status="failed", error="Purge failed. The transaction was rolled back.")
                return dict(job)
        job.update(phase="reclaiming", progress=None)
        try:
            result.update(await asyncio.to_thread(
                reclaim_purge_storage, database, progress=progress,
            ))
        except Exception:
            logger.exception("Company database disk reclamation failed")
            message = "Could not finish reclaiming disk space. Try again when the database is idle."
            if request is None:
                job.update(status="failed", error=message)
                return dict(job)
            result.update(reclaim_status="deferred", reclaim_error=message)
        job.update(status="complete", result=result)
        return dict(job)

    company_purge_database, company_purge_job = database, job
    company_purge_cancellation = cancellation
    company_purge_task = asyncio.create_task(run())
    return company_purge_task


@app.get("/api/settings/company-purge/job")
async def company_purge_status():
    if company_purge_job and company_purge_database == database_path():
        return dict(company_purge_job)
    return {"status": "idle"}


@app.post("/api/settings/company-purge/job", status_code=202)
async def company_purge_start(request: PurgeConfirmation):
    _start_company_purge(request)
    return dict(company_purge_job)


@app.post("/api/settings/company-purge/reclaim", status_code=202)
async def company_purge_reclaim():
    _start_company_purge(None)
    return dict(company_purge_job)


@app.post("/api/settings/company-purge/job/{job_id}/cancel")
async def company_purge_cancel(job_id: str):
    job = await company_purge_status()
    if job.get("id") != job_id:
        raise HTTPException(404, "This purge job is no longer available.")
    if job["status"] == "running":
        if job.get("phase") == "reclaiming":
            raise HTTPException(
                409, "Disk reclamation is underway. Committed deletions cannot be canceled.",
            )
        if not company_purge_cancellation.request():
            raise HTTPException(409, "The purge has reached commit and can no longer be canceled.")
        company_purge_job["status"] = "cancelling"
        job["status"] = "cancelling"
    return job


@app.post("/api/settings/company-purge/confirm")
async def company_purge_confirm(request: PurgeConfirmation):
    job = await asyncio.shield(_start_company_purge(request))
    if job["status"] == "failed":
        raise HTTPException(409, job["error"])
    if job["status"] == "cancelled":
        raise HTTPException(409, "The purge was canceled.")
    return job["result"]


class DashboardAssets(StaticFiles):
    async def get_response(self, path: str, scope: dict) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def dashboard_modules() -> tuple[str, str]:
    """Version the entry module by its dependency, too, to prevent mixed exports."""
    dependency = (DASHBOARD_DIR / "sales-engine.js").read_bytes()
    dependency_version = hashlib.sha256(dependency).hexdigest()[:20]
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")
    script = re.sub(r'\./sales-engine\.js\?v=[^"\s]+',
                    f"./sales-engine.js?v={dependency_version}", script)
    return script, hashlib.sha256(script.encode()).hexdigest()[:20]


@app.get("/assets/app.js", include_in_schema=False)
def dashboard_entry_module() -> Response:
    script, _ = dashboard_modules()
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/assets", DashboardAssets(directory=DASHBOARD_DIR), name="assets")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    _, version = dashboard_modules()
    css_version = hashlib.sha256((DASHBOARD_DIR / "styles.css").read_bytes()).hexdigest()[:20]
    from .oracle_deployment import REMOTE_DB
    from .release_mode import requires_user_mission

    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    if requires_user_mission():
        html = html.replace("<html ", "<html data-require-user-mission ", 1)
    if database_path() == Path(REMOTE_DB):
        html = html.replace("<html ", '<html data-storage-mode="vm" ', 1)
    html = re.sub(r'/assets/app\.js\?v=[^"\s]+', f"/assets/app.js?v={version}", html)
    html = re.sub(r'/assets/styles\.css\?v=[^"\s]+',
                  f"/assets/styles.css?v={css_version}", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def health(response: Response) -> dict:
    from .local_access import DESKTOP_API_VERSION

    response.headers["X-Beings-API-Version"] = DESKTOP_API_VERSION
    return {"status": "ok", "database": str(database_path())}


@app.get("/api/people-hydration/status")
def people_hydration_status(request: Request) -> dict[str, Any]:
    _require_loopback(request)
    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        store.connection.execute("BEGIN")
        inventory = store.treg_people_hydration_status(limit=TREG_PEOPLE_MAX_COHORT_SIZE)
    default_estimate = estimate_people_hydration(
        company_count=int(inventory["due_companies"]),
        people_limit=DEFAULT_PEOPLE_LIMIT,
        email_limit=0,
        email_max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
    )
    return {
        "configured": resolve_treg_credentials() is not None,
        "provider": "Treg",
        "source_kind": "licensed_professional_provider",
        "refresh_days": TREG_PEOPLE_REFRESH_DAYS,
        "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        "cohort_limit": TREG_PEOPLE_MAX_COHORT_SIZE,
        "defaults": {
            "people_limit": DEFAULT_PEOPLE_LIMIT,
            "email_limit": 0,
            "email_max_cost_usd": float(DEFAULT_EMAIL_MAX_COST_USD),
        },
        "inventory": inventory,
        "default_maximum_usd": default_estimate["maximum_usd"],
        "run": dict(people_hydration_manager.state),
        "disclaimer": (
            "Licensed professional provider data. Email verification does not establish "
            "permission or consent to contact."
        ),
    }


@app.post("/api/people-hydration/estimate")
def estimate_people_hydration_run(
    request: Request,
    payload: PeopleHydrationEstimateRequest,
) -> dict[str, Any]:
    _require_loopback(request)
    if people_hydration_manager.running:
        raise HTTPException(status_code=409, detail="People hydration is already running")
    return people_hydration_manager.estimate(payload)


@app.post("/api/people-hydration/runs", status_code=202)
async def start_people_hydration_run(
    request: Request,
    payload: PeopleHydrationRunStart,
) -> dict[str, Any]:
    _require_loopback(request)
    if _company_purge_active():
        raise HTTPException(409, "Wait for the company data purge to finish")
    # People scans have shared per-company scanner leases, independent of the
    # broader workflow. Keep the explicit quote and standalone-run guard below.
    return await people_hydration_manager.start(payload)


def _target_status_for_api(status: dict) -> dict:
    """Retain the detailed status contract and add stable UI-facing summary aliases."""
    inventory = status.get("inventory") or {}
    threshold = status.get("threshold") or {}
    candidates = int(inventory.get("candidates") or 0)
    inspected = int(inventory.get("inspected_candidates") or 0)
    hydrated = int(inventory.get("hydrated_latest_candidates") or 0)
    return {
        **status,
        "candidate_inventory": candidates,
        "inspected_candidates": inspected,
        "hydrated_latest_candidates": hydrated,
        "uninspected_candidates": int(inventory.get("uninspected_candidates") or 0),
        "coverage": hydrated / candidates if candidates else 0.0,
        "best_target_score": int(threshold.get("maximum_score") or 0),
        "threshold_count": int(threshold.get("at_or_above") or 0),
        "review_ready_count": int(threshold.get("review_ready") or 0),
    }


def _target_shortfall(status: dict) -> dict:
    threshold = status.get("threshold") or {}
    target_score = int(threshold.get("target_score") or 70)
    best_score = int(threshold.get("maximum_score") or 0)
    at_or_above = int(threshold.get("at_or_above") or 0)
    if at_or_above:
        message = f"{at_or_above} targets ≥{target_score}"
    elif best_score:
        message = f"Best {best_score} · {target_score - best_score} below {target_score}"
    else:
        message = f"0 targets ≥{target_score}"
    return {
        "threshold": target_score,
        "best_score": best_score,
        "at_or_above": at_or_above,
        "points_to_threshold": max(target_score - best_score, 0),
        "message": message,
    }


def _require_local_buyer_settings(request: Request) -> None:
    _require_loopback(request)
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "Settings are available only locally")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Use local Settings to change credentials")


def _buyer_secret_name(provider: str) -> str:
    names = {"treg": "TREG_TOKEN", "tiktok": "TIKTOK_COMMERCIAL_ACCESS_TOKEN"}
    if provider not in names:
        raise HTTPException(404, "Unknown buyer-intelligence provider")
    return names[provider]


def _buyer_secret_metadata(provider: str) -> dict:
    result = _secret_metadata(_buyer_secret_name(provider))
    result["source"] = "settings" if result["configured"] else "none"
    if provider == "treg" and not result["configured"]:
        credentials = resolve_treg_credentials()
        if credentials:
            result.update(configured=True, source="Treg CLI")
    return result


@app.get("/api/settings/buyer-provider/{provider}")
def buyer_provider_settings(provider: str, request: Request) -> dict:
    _require_local_buyer_settings(request)
    return _buyer_secret_metadata(provider)


@app.put("/api/settings/buyer-provider/{provider}")
def replace_buyer_provider(provider: str, request: Request, payload: SecretKeyUpdate) -> dict:
    _require_local_buyer_settings(request)
    name = _buyer_secret_name(provider)
    value = _validate_secret(payload.key)
    _write_secret(name, value)
    os.environ[name] = value
    return _buyer_secret_metadata(provider)


@app.delete("/api/settings/buyer-provider/{provider}")
def remove_buyer_provider(provider: str, request: Request) -> dict:
    _require_local_buyer_settings(request)
    name = _buyer_secret_name(provider)
    _write_secret(name, None)
    os.environ.pop(name, None)
    return _buyer_secret_metadata(provider)


@app.get("/api/settings/openai-key")
def openai_key_settings(request: Request) -> dict[str, object]:
    _require_loopback(request)
    logger.info("read OpenAI key settings configured=%s", bool(os.getenv(OPENAI_SECRET)))
    return _secret_metadata(OPENAI_SECRET)


@app.put("/api/settings/openai-key")
async def replace_openai_key(
    request: Request,
    payload: SecretKeyUpdate,
) -> dict[str, object]:
    _require_loopback(request)
    value = _validate_secret(payload.key)
    _write_secret(OPENAI_SECRET, value)
    os.environ[OPENAI_SECRET] = value
    logger.info("replaced OpenAI API key")
    resumed = discovery_method_manager.resume_queued()
    activated = await _activate_never_started_accepted_sources(
        source_kinds={"repeatable_search"},
    )
    if resumed or activated:
        logger.info(
            "OpenAI capability activated accepted source work resumed=%d queued=%d",
            resumed,
            activated,
        )
    def finish_mission_setup():
        from .missions.compiler import resume_missions_after_key_save

        with LeadStore(database_path()) as store:
            return resume_missions_after_key_save(store)

    result = _secret_metadata(OPENAI_SECRET)
    try:
        missions = await asyncio.to_thread(finish_mission_setup)
    except Exception as exc:
        logger.warning(
            "OpenAI key saved but mission setup failed error_type=%s", type(exc).__name__
        )
        missions = {"completed": 0, "error_code": "compiler_processing_failed"}
    if missions["completed"] or missions["error_code"]:
        result["mission_setup"] = missions
    return result


@app.delete("/api/settings/openai-key")
def remove_openai_key(request: Request) -> dict[str, object]:
    _require_loopback(request)
    _write_secret(OPENAI_SECRET, None)
    os.environ.pop(OPENAI_SECRET, None)
    logger.info("removed OpenAI API key")
    return _secret_metadata(OPENAI_SECRET)


@app.get("/api/settings/openai-admin-key")
def openai_admin_key_settings(request: Request) -> dict[str, object]:
    _require_local_buyer_settings(request)
    return _secret_metadata(OPENAI_ADMIN_SECRET)


@app.put("/api/settings/openai-admin-key", openapi_extra={
    "requestBody": {"required": True, "content": {
        "application/json": {"schema": SecretKeyUpdate.model_json_schema()},
    }},
})
async def replace_openai_admin_key(request: Request) -> dict[str, object]:
    _require_local_buyer_settings(request)
    # Default request-validation errors echo invalid input, including overlong secrets.
    try:
        payload = SecretKeyUpdate.model_validate(await request.json())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422, detail="Enter an API key of 1–500 characters",
        ) from None
    value = _validate_secret(payload.key)
    _write_secret(OPENAI_ADMIN_SECRET, value)
    os.environ[OPENAI_ADMIN_SECRET] = value
    return _secret_metadata(OPENAI_ADMIN_SECRET)


@app.delete("/api/settings/openai-admin-key")
def remove_openai_admin_key(request: Request) -> dict[str, object]:
    _require_local_buyer_settings(request)
    _write_secret(OPENAI_ADMIN_SECRET, None)
    os.environ.pop(OPENAI_ADMIN_SECRET, None)
    return _secret_metadata(OPENAI_ADMIN_SECRET)


@app.get("/api/settings/apify-token")
def apify_token_settings(request: Request) -> dict[str, object]:
    _require_loopback(request)
    logger.info("read Apify token settings configured=%s", bool(os.getenv(APIFY_SECRET)))
    return _secret_metadata(APIFY_SECRET)


@app.put("/api/settings/apify-token")
def replace_apify_token(request: Request, payload: SecretKeyUpdate) -> dict[str, object]:
    _require_loopback(request)
    value = _validate_secret(payload.key)
    _write_secret(APIFY_SECRET, value)
    os.environ[APIFY_SECRET] = value
    logger.info("replaced Apify API token")
    return _secret_metadata(APIFY_SECRET)


@app.delete("/api/settings/apify-token")
def remove_apify_token(request: Request) -> dict[str, object]:
    _require_loopback(request)
    _write_secret(APIFY_SECRET, None)
    os.environ.pop(APIFY_SECRET, None)
    logger.info("removed Apify API token")
    return _secret_metadata(APIFY_SECRET)


@app.get("/api/settings/microlink-key")
def microlink_key_settings(request: Request) -> dict[str, object]:
    _require_loopback(request)
    logger.info("read Microlink key settings configured=%s", bool(os.getenv(MICROLINK_SECRET)))
    return _secret_metadata(MICROLINK_SECRET)


@app.put("/api/settings/microlink-key")
def replace_microlink_key(
    request: Request,
    payload: SecretKeyUpdate,
) -> dict[str, object]:
    _require_loopback(request)
    value = _validate_secret(payload.key)
    _write_secret(MICROLINK_SECRET, value)
    os.environ[MICROLINK_SECRET] = value
    logger.info("replaced Microlink API key")
    return _secret_metadata(MICROLINK_SECRET)


@app.delete("/api/settings/microlink-key")
def remove_microlink_key(request: Request) -> dict[str, object]:
    _require_loopback(request)
    _write_secret(MICROLINK_SECRET, None)
    os.environ.pop(MICROLINK_SECRET, None)
    logger.info("removed Microlink API key")
    return _secret_metadata(MICROLINK_SECRET)


@app.get("/api/settings/dataforseo-credentials")
def dataforseo_credentials_settings(request: Request) -> dict[str, object]:
    _require_loopback(request)
    logger.info("read DataForSEO credential settings configured=%s", _dataforseo_configured())
    return _dataforseo_metadata()


@app.put("/api/settings/dataforseo-credentials")
def replace_dataforseo_credentials(
    request: Request,
    payload: DataForSEOCredentialsUpdate,
) -> dict[str, object]:
    _require_loopback(request)
    login = _validate_secret(payload.login)
    password = _validate_secret(payload.password)
    _write_secrets(
        {
            DATAFORSEO_LOGIN_SECRET: login,
            DATAFORSEO_PASSWORD_SECRET: password,
        }
    )
    os.environ[DATAFORSEO_LOGIN_SECRET] = login
    os.environ[DATAFORSEO_PASSWORD_SECRET] = password
    logger.info("replaced DataForSEO credentials")
    return _dataforseo_metadata()


@app.delete("/api/settings/dataforseo-credentials")
def remove_dataforseo_credentials(request: Request) -> dict[str, object]:
    _require_loopback(request)
    _write_secrets(
        {
            DATAFORSEO_LOGIN_SECRET: None,
            DATAFORSEO_PASSWORD_SECRET: None,
        }
    )
    os.environ.pop(DATAFORSEO_LOGIN_SECRET, None)
    os.environ.pop(DATAFORSEO_PASSWORD_SECRET, None)
    logger.info("removed DataForSEO credentials")
    return _dataforseo_metadata()


@app.get("/api/settings/treg/health")
async def treg_health_status(request: Request) -> dict:
    _require_loopback(request)
    from .treg_health import check_health

    return await check_health()


def _scan_readiness_payload() -> dict[str, object]:
    openai_ready = bool(os.getenv(OPENAI_SECRET))
    apify_ready = bool(os.getenv(APIFY_SECRET))
    microlink_ready = bool(os.getenv(MICROLINK_SECRET))
    dataforseo_ready = _dataforseo_configured()
    methods = []
    for tab in DISCOVERY_METHOD_TABS:
        requirements = list(tab.get("requires") or [])
        missing = [requirement for requirement in requirements if not os.getenv(str(requirement))]
        methods.append(
            {
                "id": tab["id"],
                "label": tab["label"],
                "ready": not missing,
                "requires": requirements,
                "missing": missing,
                "description": tab["description"],
            }
        )
    return {
        "public_web": {
            "configured": True,
            "label": "Public web methods",
            "detail": (
                "Certificate transparency, HTML/PDF crawling, OSM, Wikidata, DNS, "
                "sitemaps, feeds, ATS, Common Crawl, GDELT, and local scoring need no key."
            ),
        },
        "treg": {
            "configured": resolve_treg_credentials() is not None,
            "label": "Treg",
            "detail": "People and verified work emails; explicit lookup budgets apply.",
        },
        "openai": {
            **_secret_metadata(OPENAI_SECRET),
            "configured": openai_ready,
            "label": "OpenAI API key",
            "unlocks": [
                "Cited search recipes",
                "Blocked or name-only source resolution",
                "Source scouting",
                "Optional candidate review",
            ],
        },
        "apify": {
            **_secret_metadata(APIFY_SECRET),
            "configured": apify_ready,
            "label": "Apify API token",
            "unlocks": ["Meta/Facebook/Instagram discovery", "Optional TikTok coverage"],
        },
        "microlink": {
            **_secret_metadata(MICROLINK_SECRET),
            "configured": microlink_ready,
            "label": "Microlink API key",
            "unlocks": [
                "Rendered-HTML fallback for company pages and accepted source databases",
                "Managed proxy recovery for eligible blocking and edge failures",
            ],
        },
        "dataforseo": {
            **_dataforseo_metadata(),
            "configured": dataforseo_ready,
            "label": "DataForSEO API credentials",
            "unlocks": [
                "Top-2,500 Google visibility correction",
                "125 detailed domain rank overviews",
            ],
        },
        "methods": methods,
        "missing_items": [
            item
            for item, configured in (
                ("OpenAI API key", openai_ready),
                ("Apify API token", apify_ready),
                ("Microlink API key", microlink_ready),
                ("DataForSEO API credentials", dataforseo_ready),
            )
            if not configured
        ],
    }


@app.get("/api/settings/scan-readiness")
def scan_readiness(request: Request) -> dict[str, object]:
    _require_loopback(request)
    return _scan_readiness_payload()


@app.get("/api/ad-discovery/overview")
def ad_discovery_overview() -> dict[str, object]:
    with LeadStore(database_path()) as store:
        recent_runs = store.list_ad_discovery_runs(limit=12)
        links = store.list_advertiser_domain_links(limit=25)
        existing_target_inventory = store.ad_hydration_inventory()
    return {
        "apify": _secret_metadata(APIFY_SECRET),
        "rate_card_version": AD_RATE_CARD_VERSION,
        "rate_cards": {
            key: card.model_dump(mode="json") for key, card in default_rate_cards().items()
        },
        "coverage_notes": [
            "Meta Ad Library results include Facebook and Instagram publisher placements.",
            "TikTok Commercial Content Library is Europe-focused and is not a US census.",
            "TikTok Top Ads is curated positive-only evidence.",
            "No-result and unresolved mappings never subtract score.",
        ],
        "recent_runs": recent_runs,
        "links": links,
        "existing_target_inventory": existing_target_inventory,
        "status": ad_discovery_manager.status_payload(),
    }


@app.post("/api/ad-discovery/estimates")
def estimate_ad_discovery(payload: AdDiscoveryRequest) -> dict[str, object]:
    return ad_discovery_manager.create_quote(payload)


@app.post("/api/ad-discovery/runs", status_code=202)
async def start_ad_discovery(payload: AdDiscoveryRunStart) -> dict[str, object]:
    _reject_new_work_during_smart_scan()
    return await ad_discovery_manager.start(payload)


@app.get("/api/ad-discovery/status")
def ad_discovery_status() -> dict[str, object]:
    return ad_discovery_manager.status_payload()


@app.get("/api/ad-discovery/runs/{run_id}")
def ad_discovery_run(run_id: int) -> dict[str, object]:
    with LeadStore(database_path()) as store:
        run = store.get_ad_discovery_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Paid-ad run not found")
    return {"run": run}


@app.post("/api/ad-discovery/runs/{run_id}/cancel", status_code=202)
async def cancel_ad_discovery(run_id: int) -> dict[str, object]:
    return await ad_discovery_manager.cancel(run_id)


@app.get("/api/ad-discovery/links")
def advertiser_domain_links(
    state: str = Query(default="", max_length=20),
    platform: str = Query(default="", max_length=20),
    candidate_id: int | None = Query(default=None, ge=1),
    unlinked: bool | None = Query(default=None),
    search: str = Query(default="", max_length=300),
    limit: int = Query(default=100, ge=1, le=1_000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    with LeadStore(database_path()) as store:
        try:
            return store.list_advertiser_domain_links(
                state=state,
                platform=platform,
                candidate_id=candidate_id,
                unlinked=unlinked,
                search=search,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def _creative_surface(creative: dict[str, object]) -> tuple[str, str, str]:
    platform = str(creative.get("platform") or "").lower()
    coverage = str(creative.get("coverage_type") or "").lower()
    if platform == "meta":
        return ("meta", "meta_library", "meta_public_ad_library")
    if "top" in coverage:
        return ("tiktok", "tiktok_top", "curated_top_ads_positive_only")
    return ("tiktok", "tiktok_library", "europe_commercial_content_library")


def _refresh_reviewed_candidate_ads(
    store: LeadStore,
    candidate_id: int,
    *,
    affected_advertiser_id: int,
) -> None:
    """Re-aggregate current evidence so a review takes effect without another paid scan."""
    latest_run = store.connection.execute(
        "SELECT id FROM ad_discovery_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if latest_run is None:
        return
    run_id = int(latest_run["id"])
    verified_links = store.list_advertiser_domain_links(
        candidate_id=candidate_id,
        state="verified",
        limit=1_000,
    )["items"]
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    confidences: dict[tuple[str, str, str], int] = {}
    affected_surfaces = {
        _creative_surface(item) for item in store.advertiser_creatives(affected_advertiser_id)
    }
    for link in verified_links:
        confidence = int(link.get("confidence") or 0)
        for creative in store.advertiser_creatives(int(link["advertiser_id"])):
            key = _creative_surface(creative)
            groups.setdefault(key, []).append(creative)
            confidences[key] = max(confidences.get(key, 0), confidence)
            affected_surfaces.add(key)
    now = datetime.now(UTC)
    notes = {
        "meta_library": "Meta Library covers Facebook and Instagram publisher placements.",
        "tiktok_library": "TikTok Library is Europe-focused; absence is not negative evidence.",
        "tiktok_top": "TikTok Top Ads is curated positive-only evidence.",
    }
    for platform, surface, coverage in affected_surfaces:
        creatives = groups.get((platform, surface, coverage), [])
        if not creatives:
            store.record_candidate_ad_observation(
                candidate_id,
                run_id,
                platform=platform,
                surface=surface,
                coverage_type=coverage,
                identity_state="rejected",
                identity_confidence=0,
                coverage_notes=[notes[surface]],
                metadata={"manual_mapping_review": True},
            )
            continue
        unique = {str(item.get("external_id") or item.get("id")): item for item in creatives}
        creatives = list(unique.values())
        active = [item for item in creatives if item.get("is_active")]
        group_ids = list(
            dict.fromkeys(
                str(
                    item.get("creative_group_id")
                    or item.get("creative_fingerprint")
                    or item.get("external_id")
                )
                for item in creatives
            )
        )
        active_group_ids = list(
            dict.fromkeys(
                str(
                    item.get("creative_group_id")
                    or item.get("creative_fingerprint")
                    or item.get("external_id")
                )
                for item in active
            )
        )
        started: list[datetime] = []
        for item in creatives:
            try:
                value = datetime.fromisoformat(str(item.get("started_at") or ""))
            except ValueError:
                continue
            started.append(value if value.tzinfo else value.replace(tzinfo=UTC))
        publishers = list(
            dict.fromkeys(
                str(value).lower()
                for item in creatives
                for value in (item.get("publisher_platforms") or [])
                if str(value).strip()
            )
        )
        source_urls = list(
            dict.fromkeys(
                str(item.get("source_url"))
                for item in creatives
                if str(item.get("source_url") or "").startswith(("http://", "https://"))
            )
        )
        store.record_candidate_ad_observation(
            candidate_id,
            run_id,
            platform=platform,
            surface=surface,
            coverage_type=coverage,
            identity_state="verified",
            identity_confidence=max(80, confidences.get((platform, surface, coverage), 80)),
            total_ads=len(creatives),
            active_ads=len(active),
            unique_creatives=len(group_ids),
            active_unique_creatives=len(active_group_ids),
            video_creatives=sum(item.get("media_type") == "video" for item in creatives),
            image_creatives=sum(item.get("media_type") == "image" for item in creatives),
            new_creatives_7d=sum(value >= now - timedelta(days=7) for value in started),
            new_creatives_30d=sum(value >= now - timedelta(days=30) for value in started),
            new_creatives_90d=sum(value >= now - timedelta(days=90) for value in started),
            first_started_at=min(started).isoformat() if started else None,
            latest_started_at=max(started).isoformat() if started else None,
            platforms=publishers,
            creative_group_ids=group_ids,
            active_creative_group_ids=active_group_ids,
            source_urls=source_urls,
            coverage_notes=[notes[surface]],
            metadata={"manual_mapping_review": True},
        )
    store.reproject_candidate_ad_intelligence(candidate_id)


@app.patch("/api/ad-discovery/links/{link_id}")
def review_advertiser_domain_link(
    link_id: int,
    payload: AdDomainLinkReview,
) -> dict[str, object]:
    with LeadStore(database_path()) as store:
        link = store.get_advertiser_domain_link(link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="Advertiser-domain mapping not found")
        prior_candidate_id = link.get("candidate_id")
        candidate_id = prior_candidate_id
        operator_admission = False
        if payload.state == "verified" and candidate_id is None:
            advertiser = link["advertiser"]
            existing_candidate = store.find_candidate_by_canonical_domain(link["domain"])
            if existing_candidate is not None:
                candidate_id = int(existing_candidate["candidate_id"])
            else:
                operator_admission = True
            evidence_urls: list[str] = []
            for item in link.get("evidence") or []:
                value = (
                    item
                    if isinstance(item, str)
                    else item.get("url") or item.get("source_url") or ""
                )
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    evidence_urls.append(value)
            if advertiser.get("profile_url"):
                evidence_urls.append(advertiser["profile_url"])
            source = (
                "meta-ad-library" if advertiser.get("platform") == "meta" else "tiktok-ad-library"
            )
            discovery_hostname = (
                str(existing_candidate["hostname"])
                if existing_candidate is not None
                else link["domain"]
            )
            store.upsert_derived_candidates(
                [
                    DiscoveredHost(
                        source_apex=source,
                        hostname=discovery_hostname,
                        label_hint=advertiser.get("display_name") or "",
                        discovery_rank=1.0,
                        discovery_evidence_urls=list(dict.fromkeys(evidence_urls))[:20],
                        provider_record_ids=[
                            f"{advertiser.get('platform')}:{advertiser.get('external_id')}"
                        ],
                    )
                ]
            )
            if candidate_id is None:
                candidate_id = store.candidate_ids([link["domain"]]).get(link["domain"])
        reviewed = store.review_advertiser_domain_link(
            link_id,
            state=payload.state,
            reviewer=payload.reviewer,
            note=payload.review_note,
            confidence=(
                max(80, int(link.get("confidence") or 0))
                if payload.state == "verified"
                else int(link.get("confidence") or 0)
            ),
            candidate_id=int(candidate_id) if candidate_id is not None else None,
        )
        affected_candidate_id = candidate_id or prior_candidate_id
        if affected_candidate_id is not None:
            _refresh_reviewed_candidate_ads(
                store,
                int(affected_candidate_id),
                affected_advertiser_id=int(link["advertiser_id"]),
            )
    if operator_admission and candidate_id is not None and smart_scan_manager.active:
        excluded = smart_scan_manager.state.setdefault("new_company_excluded_ids", [])
        if int(candidate_id) not in excluded:
            excluded.append(int(candidate_id))
        smart_scan_manager._persist_state()
    if reviewed is None:  # pragma: no cover - fetched immediately above
        raise HTTPException(status_code=404, detail="Advertiser-domain mapping not found")
    return {"link": reviewed}


@app.get("/api/dashboard")
def dashboard() -> dict:
    with LeadStore(database_path()) as store:
        payload = store.dashboard_payload()
        raw_target_status = store.target_intelligence_status()
        payload["target_intelligence"] = _target_status_for_api(raw_target_status)
        payload["target_shortfall"] = _target_shortfall(raw_target_status)
        payload["top_buyer_targets"] = store.list_targets(lane="buyer", scope="direct", limit=20)[
            "items"
        ]
        payload["top_referral_partners"] = store.list_targets(
            lane="referral_partner", scope="all", limit=20
        )["items"]
    payload["capabilities"] = {
        "ai": bool(os.getenv("OPENAI_API_KEY")),
        "source_scout": bool(os.getenv("OPENAI_API_KEY")),
        "auto_scout_hours": int(os.getenv("BEINGS_LEADS_AUTO_SCOUT_HOURS", "0") or 0),
        "registry_path": str(REGISTRY_PATH),
    }
    return payload


def _account_error(exc: ValueError, *, candidate_id: int | None = None) -> HTTPException:
    if isinstance(exc, AccountConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if candidate_id is not None and str(exc) == "Candidate not found":
        return HTTPException(status_code=404, detail="Candidate not found")
    return HTTPException(status_code=422, detail=str(exc))


def _csv_download(output: io.StringIO, filename: str) -> Response:
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/account-dashboard")
def account_dashboard(cohort: str = "prototype-v1") -> dict:
    with LeadStore(database_path()) as store:
        return store.account_dashboard(cohort=cohort.strip() or "prototype-v1")


@app.post("/api/accounts/import.csv", response_model=None)
async def import_accounts_csv(
    request: Request,
    dry_run: bool = True,
) -> dict | JSONResponse:
    body = await request.body()
    if len(body) > MAX_ACCOUNT_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"CSV import must be at most {MAX_ACCOUNT_IMPORT_BYTES} bytes",
        )
    try:
        content = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV import must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV import requires a header row")
    fieldnames = [
        re.sub(r"[^a-z0-9]+", "_", (field or "").strip().lower()).strip("_")
        for field in reader.fieldnames
    ]
    if any(not field for field in fieldnames) or len(set(fieldnames)) != len(fieldnames):
        raise HTTPException(status_code=422, detail="CSV headers must be unique and non-empty")
    aliases = {"source": "lead_source", "relationship": "relationship_type"}
    reader.fieldnames = [aliases.get(field, field) for field in fieldnames]
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise HTTPException(
            status_code=422,
            detail="CSV headers become duplicates after alias normalization",
        )
    unsupported = sorted(set(reader.fieldnames) - set(AccountCreate.model_fields))
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported CSV columns: {', '.join(unsupported)}",
        )
    try:
        raw_rows = [
            row for row in reader if any(str(value or "").strip() for value in row.values())
        ]
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail=f"Malformed CSV: {exc}") from exc
    if not raw_rows:
        raise HTTPException(status_code=422, detail="CSV import contains no account rows")
    if len(raw_rows) > MAX_ACCOUNT_IMPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=f"CSV import is limited to {MAX_ACCOUNT_IMPORT_ROWS} account rows",
        )

    rows: list[dict] = []
    errors: list[dict[str, object]] = []
    defaultable = {
        "candidate_id",
        "cohort",
        "relationship_type",
        "segment",
        "lead_source",
        "intro_status",
    }
    for row_number, raw in enumerate(raw_rows, start=2):
        if raw.get(None):
            errors.append({"row": row_number, "error": "Row contains more values than the header"})
            continue
        values = {key: str(value or "").strip() for key, value in raw.items() if key is not None}
        values = {key: value for key, value in values.items() if value or key not in defaultable}
        try:
            rows.append(AccountCreate.model_validate(values).model_dump())
        except ValidationError as exc:
            message = "; ".join(error["msg"] for error in exc.errors(include_url=False))
            errors.append({"row": row_number, "error": message})
    if errors:
        result = {
            "dry_run": dry_run,
            "valid": False,
            "total": len(raw_rows),
            "created": 0,
            "existing": 0,
            "rows": [{**error, "action": "error"} for error in errors],
            "errors": errors,
        }
        return result if dry_run else JSONResponse(status_code=422, content=result)

    with LeadStore(database_path()) as store:
        result = store.import_accounts(rows, dry_run=dry_run)
    if not result["valid"] and not dry_run:
        return JSONResponse(status_code=422, content=result)
    return result


@app.get("/api/accounts/export.csv")
def export_accounts_csv(cohort: str = "prototype-v1") -> Response:
    with LeadStore(database_path()) as store:
        accounts = store.list_accounts(cohort=cohort, limit=10_000)["items"]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "account_id",
            "candidate_id",
            "company",
            "website",
            "cohort",
            "relationship_type",
            "segment",
            "lead_source",
            "referrer",
            "intro_status",
            "qualification_state",
            "qualification_score",
            "confidence",
            "buyer_name",
            "buyer_role",
            "why_now",
            "first_month_asks",
            "lifecycle",
            "outreach_outcome",
            "proposal_outcome",
            "lost_reason",
            "renewal_decision",
            "delivery_burden",
            "notes",
            "created_at",
            "updated_at",
        ]
    )
    for account in accounts:
        buyer = account["qualification"]["buyer"] or {}
        writer.writerow(
            [
                account["id"],
                account["candidate_id"] or "",
                _csv_cell(account["company"]),
                _csv_cell(account["website"]),
                _csv_cell(account["cohort"]),
                _csv_cell(account["relationship_type"]),
                _csv_cell(account["segment"]),
                _csv_cell(account["lead_source"]),
                _csv_cell(account["referrer"]),
                _csv_cell(account["intro_status"]),
                account["qualification_state"],
                account["qualification_score"],
                _csv_cell(account["qualification"]["confidence"]),
                _csv_cell(buyer.get("name", "")),
                _csv_cell(buyer.get("role", "")),
                _csv_cell(account["qualification"]["why_now"]),
                _csv_cell("; ".join(account["qualification"]["first_month_asks"])),
                _csv_cell(account["lifecycle"]),
                _csv_cell(account["outcome"]["outreach_outcome"]),
                _csv_cell(account["outcome"]["proposal_outcome"]),
                _csv_cell(account["outcome"]["lost_reason"]),
                _csv_cell(account["delivery"]["renewal_decision"]),
                _csv_cell(account["delivery"]["delivery_burden"]),
                _csv_cell(account["notes"]),
                account["created_at"],
                account["updated_at"],
            ]
        )
    return _csv_download(output, f"beings-accounts-{datetime.now(UTC).date().isoformat()}.csv")


@app.get("/api/accounts/evidence.csv")
def export_account_evidence_csv(cohort: str = "prototype-v1") -> Response:
    with LeadStore(database_path()) as store:
        accounts = store.list_accounts(cohort=cohort, limit=10_000)["items"]
        details = [store.account_detail(account["id"]) for account in accounts]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "account_id",
            "company",
            "website",
            "claim_id",
            "claim",
            "epistemic_status",
            "truth_state",
            "signals",
            "gates",
            "dimensions",
            "source_kind",
            "source_url",
            "observed_at",
            "confidence",
            "human_verified",
            "verified_by",
            "verified_at",
        ]
    )
    for account in details:
        if account is None:
            continue
        for claim in account["claims"]:
            writer.writerow(
                [
                    account["id"],
                    _csv_cell(account["company"]),
                    _csv_cell(account["website"]),
                    claim["id"],
                    _csv_cell(claim["claim"]),
                    _csv_cell(claim["epistemic_status"]),
                    _csv_cell(claim["truth_state"]),
                    _csv_cell("; ".join(claim["signals"])),
                    _csv_cell("; ".join(claim["gates"])),
                    _csv_cell("; ".join(claim["dimensions"])),
                    _csv_cell(claim["source_kind"]),
                    _csv_cell(claim["source_url"]),
                    claim["observed_at"],
                    _csv_cell(claim["confidence"]),
                    "yes" if claim["human_verified"] else "no",
                    _csv_cell(claim["verified_by"]),
                    claim["verified_at"] or "",
                ]
            )
    return _csv_download(
        output, f"beings-account-evidence-{datetime.now(UTC).date().isoformat()}.csv"
    )


@app.get("/api/accounts/outcomes.csv")
def export_account_outcomes_csv(cohort: str = "prototype-v1") -> Response:
    with LeadStore(database_path()) as store:
        accounts = store.list_accounts(cohort=cohort, limit=10_000)["items"]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "account_id",
            "company",
            "segment",
            "lead_source",
            "outreach_outcome",
            "proposal_outcome",
            "proposal_monthly_value",
            "won_at",
            "lost_at",
            "lost_reason",
            "month_one_started_at",
            "assets_delivered",
            "assets_used",
            "delivered_to_used_rate",
            "revision_rounds",
            "revision_hours",
            "internal_hours_displaced",
            "vendor_coordination_hours_displaced",
            "month_two_queue_depth",
            "recognized_revenue",
            "direct_delivery_cost",
            "contribution_margin",
            "delivery_burden",
            "day_21_renewal_intent",
            "renewal_decision",
            "renewal_decided_at",
            "outcome_notes",
            "delivery_notes",
        ]
    )
    for account in accounts:
        outcome = account["outcome"]
        delivery = account["delivery"]
        writer.writerow(
            [
                account["id"],
                _csv_cell(account["company"]),
                _csv_cell(account["segment"]),
                _csv_cell(account["lead_source"]),
                _csv_cell(outcome["outreach_outcome"]),
                _csv_cell(outcome["proposal_outcome"]),
                (
                    outcome["proposal_monthly_value"]
                    if outcome["proposal_monthly_value"] is not None
                    else ""
                ),
                outcome["won_at"] or "",
                outcome["lost_at"] or "",
                _csv_cell(outcome["lost_reason"]),
                outcome["month_one_started_at"] or "",
                delivery["assets_delivered"],
                delivery["assets_used"],
                (
                    account["delivered_to_used_rate"]
                    if account["delivered_to_used_rate"] is not None
                    else ""
                ),
                delivery["revision_rounds"],
                delivery["revision_hours"],
                delivery["internal_hours_displaced"],
                delivery["vendor_coordination_hours_displaced"],
                delivery["month_two_queue_depth"],
                (
                    delivery["recognized_revenue"]
                    if delivery["recognized_revenue"] is not None
                    else ""
                ),
                (
                    delivery["direct_delivery_cost"]
                    if delivery["direct_delivery_cost"] is not None
                    else ""
                ),
                (
                    delivery["contribution_margin"]
                    if delivery["contribution_margin"] is not None
                    else ""
                ),
                _csv_cell(delivery["delivery_burden"]),
                _csv_cell(delivery["day_21_renewal_intent"]),
                _csv_cell(delivery["renewal_decision"]),
                delivery["renewal_decided_at"] or "",
                _csv_cell(outcome["notes"]),
                _csv_cell(delivery["notes"]),
            ]
        )
    return _csv_download(
        output, f"beings-account-outcomes-{datetime.now(UTC).date().isoformat()}.csv"
    )


@app.get("/api/accounts/pipeline.md")
def account_pipeline_markdown(cohort: str = "prototype-v1") -> Response:
    with LeadStore(database_path()) as store:
        content = store.account_pipeline_markdown(cohort=cohort)
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="beings-pipeline-{datetime.now(UTC).date().isoformat()}.md"'
            )
        },
    )


@app.get("/api/accounts")
def accounts(
    cohort: str = "prototype-v1",
    search: str = "",
    relationship_type: Literal["buyer", "referral_partner"] | None = None,
    segment: AccountSegment | None = None,
    lead_source: AccountLeadSource | None = None,
    qualification_state: Literal["pursue", "needs_research", "disqualified"] | None = None,
    lifecycle: AccountLifecycle | None = None,
    min_score: int = Query(default=0, ge=0, le=100),
    sort: Literal["score", "updated", "company"] = "score",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with LeadStore(database_path()) as store:
        return store.list_accounts(
            cohort=cohort.strip(),
            search=search,
            relationship_type=relationship_type or "",
            segment=segment or "",
            lead_source=lead_source or "",
            qualification_state=qualification_state or "",
            lifecycle=lifecycle or "",
            min_score=min_score,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@app.post("/api/accounts", status_code=201)
def create_account(payload: AccountCreate, response: Response) -> dict:
    try:
        with LeadStore(database_path()) as store:
            account, created = store.create_account(**payload.model_dump())
    except ValueError as exc:
        raise _account_error(exc, candidate_id=payload.candidate_id) from exc
    response.status_code = 201 if created else 200
    return {**account, "created": created}


@app.get("/api/accounts/{account_id}")
def account_detail(account_id: int) -> dict:
    with LeadStore(database_path()) as store:
        account = store.account_detail(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: int, patch: AccountPatch) -> dict:
    changes = {
        key: value
        for key, value in patch.model_dump(exclude_unset=True).items()
        if value is not None
    }
    try:
        with LeadStore(database_path()) as store:
            account = store.patch_account(account_id, changes)
    except ValueError as exc:
        raise _account_error(exc) from exc
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.put("/api/accounts/{account_id}/qualification")
def update_account_qualification(
    account_id: int,
    qualification: StrategicQualification,
) -> dict:
    with LeadStore(database_path()) as store:
        account = store.save_qualification(account_id, qualification)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.get("/api/accounts/{account_id}/claims")
def account_claims(account_id: int) -> dict:
    with LeadStore(database_path()) as store:
        claims = store.list_claims(account_id)
    if claims is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"items": claims, "total": len(claims)}


@app.post("/api/accounts/{account_id}/claims", status_code=201)
def create_account_claim(account_id: int, claim: EvidenceClaim) -> dict:
    with LeadStore(database_path()) as store:
        created = store.create_claim(account_id, claim)
        account = store.account_detail(account_id)
    if created is None or account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"claim": created, "account": account}


@app.patch("/api/accounts/{account_id}/claims/{claim_id}")
def update_account_claim(account_id: int, claim_id: int, patch: ClaimPatch) -> dict:
    with LeadStore(database_path()) as store:
        claims = store.list_claims(account_id)
        if claims is None:
            raise HTTPException(status_code=404, detail="Account not found")
        existing = next((item for item in claims if item["id"] == claim_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="Claim not found")
        try:
            claim = EvidenceClaim.model_validate(
                {**existing, **patch.model_dump(exclude_unset=True)}
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
        updated = store.update_claim(account_id, claim_id, claim)
        account = store.account_detail(account_id)
    return {"claim": updated, "account": account}


@app.delete("/api/accounts/{account_id}/claims/{claim_id}")
def delete_account_claim(account_id: int, claim_id: int) -> dict:
    with LeadStore(database_path()) as store:
        deleted = store.delete_claim(account_id, claim_id)
        account = store.account_detail(account_id) if deleted else None
    if deleted is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if deleted is False:
        raise HTTPException(status_code=404, detail="Claim not found")
    return {"deleted": claim_id, "account": account}


@app.put("/api/accounts/{account_id}/capacity-map")
def update_account_capacity_map(account_id: int, capacity_map: CreativeCapacityMap) -> dict:
    with LeadStore(database_path()) as store:
        account = store.save_capacity_map(account_id, capacity_map)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.get("/api/accounts/{account_id}/capacity-map.md")
def download_account_capacity_map(account_id: int) -> Response:
    with LeadStore(database_path()) as store:
        account = store.account_detail(account_id)
        content = store.capacity_map_markdown(account_id)
    if account is None or content is None:
        raise HTTPException(status_code=404, detail="Account not found")
    slug = re.sub(r"[^a-z0-9]+", "-", account["company"].lower()).strip("-")[:60]
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{slug or "account"}-capacity-map.md"'
        },
    )


@app.put("/api/accounts/{account_id}/discovery")
def update_account_discovery(account_id: int, discovery: DiscoveryReview) -> dict:
    try:
        with LeadStore(database_path()) as store:
            account = store.save_discovery(account_id, discovery)
    except AccountConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.put("/api/accounts/{account_id}/outcome")
def update_account_outcome(account_id: int, outcome: SalesOutcome) -> dict:
    try:
        with LeadStore(database_path()) as store:
            account = store.save_outcome(account_id, outcome)
    except AccountConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@app.put("/api/accounts/{account_id}/delivery")
def update_account_delivery(account_id: int, delivery: DeliveryOutcome) -> dict:
    try:
        with LeadStore(database_path()) as store:
            account = store.save_delivery(account_id, delivery)
    except AccountConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _list_targets(
    *,
    lane: TargetLane,
    scope: TargetScope,
    min_score: int,
    min_principal_signals: int,
    review_ready: bool | None,
    search: str,
    archetype: str,
    sort: TargetSort,
    limit: int,
    offset: int,
) -> dict:
    with LeadStore(database_path()) as store:
        result = store.list_targets(
            lane=lane,
            scope=scope,
            min_score=min_score,
            min_principal_signals=min_principal_signals,
            review_ready=review_ready,
            search=search,
            archetype=archetype,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        raw_status = store.target_intelligence_status()
        generation = store.target_data_generation()
    return {
        **result,
        "generation": generation,
        "status": _target_status_for_api(raw_status),
        "shortfall": _target_shortfall(raw_status),
    }


@app.get("/api/targets/status")
def target_intelligence_status() -> dict:
    with LeadStore(database_path()) as store:
        raw_status = store.target_intelligence_status()
        generation = store.target_data_generation()
    return {
        "generation": generation,
        "status": _target_status_for_api(raw_status),
        "shortfall": _target_shortfall(raw_status),
    }


@app.get("/api/targets/overview")
def target_overview(
    limit: int = Query(default=20, ge=1, le=100),
    include_status: bool = Query(default=True),
    sort: TargetSort = "priority",
) -> dict:
    """Return exactly the target data needed for the initial overview render."""
    with LeadStore(database_path()) as store:
        overview_options = {
            "include_total": False,
            "include_aliases": False,
            "include_ad_intelligence": False,
            "include_reliability": False,
        }
        buyers = store.list_targets(
            lane="buyer", scope="direct", sort=sort, limit=limit, **overview_options
        )["items"]
        referrals = store.list_targets(
            lane="referral_partner", scope="all", sort=sort, limit=limit, **overview_options
        )["items"]
        raw_status = store.target_intelligence_status() if include_status else None
        generation = store.target_data_generation()
    payload = {
        "generation": generation,
        "top_buyer_targets": buyers,
        "top_referral_partners": referrals,
    }
    if raw_status is not None:
        payload["target_intelligence"] = _target_status_for_api(raw_status)
        payload["target_shortfall"] = _target_shortfall(raw_status)
    return payload


@app.get("/api/targets")
def targets(
    lane: TargetLane = "buyer",
    scope: TargetScope = "direct",
    min_score: int = Query(default=0, ge=0, le=100),
    min_principal_signals: int = Query(default=0, ge=0, le=3),
    review_ready: bool | None = None,
    search: str = "",
    archetype: str = "",
    sort: TargetSort = "priority",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return _list_targets(
        lane=lane,
        scope=scope,
        min_score=min_score,
        min_principal_signals=min_principal_signals,
        review_ready=review_ready,
        search=search,
        archetype=archetype,
        sort=sort,
        limit=limit,
        offset=offset,
    )


def _target_json_cell(value: object) -> str:
    return _csv_cell(json.dumps(value, ensure_ascii=False, sort_keys=True))


@app.get("/api/targets/export.csv")
def export_targets(
    lane: TargetLane = "buyer",
    scope: TargetScope = "direct",
    min_score: int = Query(default=0, ge=0, le=100),
    min_principal_signals: int = Query(default=0, ge=0, le=3),
    review_ready: bool | None = None,
    search: str = "",
    archetype: str = "",
) -> Response:
    filters = {
        "lane": lane,
        "scope": scope,
        "min_score": min_score,
        "min_principal_signals": min_principal_signals,
        "review_ready": review_ready,
        "search": search,
        "archetype": archetype,
    }
    items: list[dict] = []
    offset = 0
    with LeadStore(database_path()) as store:
        while True:
            page = store.list_targets(**filters, limit=1_000, offset=offset)
            batch = page["items"]
            items.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page["total"]):
                break

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    principal_signals = (
        "distribution_investment",
        "recurring_creative_demand",
        "production_capacity_gap",
    )
    dimensions = (
        "distribution_investment",
        "recurring_creative_demand",
        "production_capacity_gap",
        "ai_brand_readiness",
        "timing_access",
    )
    writer.writerow(
        [
            "rank",
            "candidate_id",
            "company",
            "website",
            "canonical_domain",
            "hostname",
            "source",
            "relationship_lane",
            "target_scope",
            "archetype",
            "target_score",
            "founder_priority_score",
            "durable_fit_score",
            "contact_now_score",
            "recommended_action",
            "next_best_scan",
            "review_ready",
            "principal_signals_met",
            "principal_signals_unknown",
            "data_confidence",
            "observed_at",
            "rubric_version",
            "why_now",
            "contact_window",
            "conditional_gap",
            "call_opener",
            "email_subject",
            "email_angle",
            "first_month_asks_json",
            "discovery_questions_json",
            "likely_objection",
            "safe_response",
            "do_not_claim_json",
            "buyer_roles_json",
            "public_people_json",
            "opportunity_json",
            "signal_events_json",
            "signal_clusters_json",
            *[f"axis_{name}" for name in dimensions],
            *[
                field
                for name in principal_signals
                for field in (
                    f"signal_{name}_truth",
                    f"signal_{name}_epistemic",
                    f"signal_{name}_confidence",
                    f"signal_{name}_summary",
                    f"signal_{name}_evidence_urls",
                )
            ],
            "derived_signals",
            "observations_json",
            "inferences_json",
            "blockers_json",
            "criteria_gaps_json",
            "unassessable_criteria_json",
            "manual_gates_json",
            "leading_gap_json",
            "discovery_evidence_urls",
            "identity_aliases_json",
        ]
    )
    for item in items:
        target = item["target_intelligence"]
        target_dimensions = target.get("dimensions") or {}
        decision_packet = target.get("decision_packet") or {}
        signals = target.get("principal_signals") or {}
        signal_cells: list[object] = []
        for name in principal_signals:
            signal = signals.get(name) or {}
            signal_cells.extend(
                [
                    _csv_cell(signal.get("truth_state", "unknown")),
                    _csv_cell(signal.get("epistemic_status", "unknown")),
                    _csv_cell(signal.get("confidence", "low")),
                    _csv_cell(signal.get("summary", "")),
                    _csv_cell("; ".join(signal.get("evidence_urls") or [])),
                ]
            )
        writer.writerow(
            [
                item["rank"],
                item["candidate_id"],
                _csv_cell(item["company_name"]),
                _csv_cell(item["company_url"]),
                _csv_cell(item["canonical_domain"]),
                _csv_cell(item["hostname"]),
                _csv_cell(item["source_apex"]),
                _csv_cell(item["relationship_lane"]),
                _csv_cell(item["target_scope"]),
                _csv_cell(item["archetype"]),
                item["target_score"],
                target.get("founder_priority_score", 0),
                target.get("durable_fit_score", 0),
                target.get("contact_now_score", 0),
                _csv_cell(target.get("recommended_action", "research_next")),
                _csv_cell(target.get("next_best_scan", "none")),
                "yes" if item["review_ready"] else "no",
                item["principal_signals_met"],
                _csv_cell("; ".join(target.get("principal_signals_unknown") or [])),
                item["data_confidence"],
                item["observed_at"] or "",
                _csv_cell(target["rubric_version"]),
                _csv_cell(target.get("why_now", "")),
                _csv_cell(decision_packet.get("contact_window", "")),
                _csv_cell(decision_packet.get("conditional_gap", "")),
                _csv_cell(decision_packet.get("call_opener", "")),
                _csv_cell(decision_packet.get("email_subject", "")),
                _csv_cell(decision_packet.get("email_angle", "")),
                _target_json_cell(decision_packet.get("first_month_asks") or []),
                _target_json_cell(decision_packet.get("discovery_questions") or []),
                _csv_cell(decision_packet.get("likely_objection", "")),
                _csv_cell(decision_packet.get("safe_response", "")),
                _target_json_cell(decision_packet.get("do_not_claim") or []),
                _target_json_cell(decision_packet.get("buyer_roles") or []),
                _target_json_cell(decision_packet.get("public_people") or []),
                _target_json_cell(target.get("opportunity") or {}),
                _target_json_cell(target.get("signal_events") or []),
                _target_json_cell(target.get("signal_clusters") or []),
                *[target_dimensions.get(name, 0) for name in dimensions],
                *signal_cells,
                _csv_cell("; ".join(target.get("derived_signals") or [])),
                _target_json_cell(target.get("observations") or []),
                _target_json_cell(target.get("inferences") or []),
                _target_json_cell(target.get("blockers") or []),
                _target_json_cell(target.get("criteria_gaps") or []),
                _target_json_cell(target.get("unassessable_criteria") or []),
                _target_json_cell(target.get("manual_gates") or []),
                _target_json_cell(target.get("leading_gap")),
                _csv_cell("; ".join(item.get("discovery_evidence_urls") or [])),
                _target_json_cell(target.get("identity_aliases") or []),
            ]
        )
    return _csv_download(output, f"beings-targets-{datetime.now(UTC).date().isoformat()}.csv")


@app.get("/api/prospects")
def prospects(
    search: str = "",
    source: str = "",
    stage: str = "",
    tag: str = "",
    min_score: int = Query(default=0, ge=0, le=100),
    live: bool | None = None,
    starred: bool | None = None,
    exclusion: Literal["active", "excluded", "all"] = "active",
    budget_ready: bool | None = None,
    placement_ready: bool | None = None,
    has_public_email: bool | None = None,
    archetype: str = "",
    min_placement: int = Query(default=0, ge=0, le=100),
    min_demand: int = Query(default=0, ge=0, le=100),
    min_capacity: int = Query(default=0, ge=0, le=100),
    min_timing: int = Query(default=0, ge=0, le=100),
    min_creative_capacity: int = Query(default=0, ge=0, le=100),
    creative_review_ready: bool | None = None,
    sort: Literal[
        "placement",
        "priority",
        "demand",
        "capacity",
        "timing",
        "creative_capacity",
        "growth",
        "confidence",
        "vendor_stack",
        "creative_stack",
        "osint",
        "fit",
        "changed",
    ] = "placement",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with LeadStore(database_path()) as store:
        return store.list_prospects(
            search=search,
            source=source,
            stage=stage,
            tag=tag,
            min_score=min_score,
            live=live,
            starred=starred,
            excluded={"active": False, "excluded": True, "all": None}[exclusion],
            budget_ready=budget_ready,
            placement_ready=placement_ready,
            has_public_email=has_public_email,
            archetype=archetype,
            min_placement=min_placement,
            min_demand=min_demand,
            min_capacity=min_capacity,
            min_timing=min_timing,
            min_creative_capacity=min_creative_capacity,
            creative_review_ready=creative_review_ready,
            sort=sort,
            limit=limit,
            offset=offset,
        )


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.lstrip().startswith(("=", "+", "-", "@")) else text


@app.get("/api/prospects/export.csv")
def export_prospects(
    search: str = "",
    source: str = "",
    stage: str = "",
    tag: str = "",
    min_score: int = Query(default=0, ge=0, le=100),
    live: bool | None = None,
    starred: bool | None = None,
    exclusion: Literal["active", "excluded", "all"] = "active",
    budget_ready: bool | None = None,
    placement_ready: bool | None = None,
    has_public_email: bool | None = None,
    archetype: str = "",
    min_placement: int = Query(default=0, ge=0, le=100),
    min_demand: int = Query(default=0, ge=0, le=100),
    min_capacity: int = Query(default=0, ge=0, le=100),
    min_timing: int = Query(default=0, ge=0, le=100),
    min_creative_capacity: int = Query(default=0, ge=0, le=100),
    creative_review_ready: bool | None = None,
    sort: Literal[
        "placement",
        "priority",
        "demand",
        "capacity",
        "timing",
        "creative_capacity",
        "growth",
        "confidence",
        "vendor_stack",
        "creative_stack",
        "osint",
        "fit",
        "changed",
    ] = "placement",
) -> Response:
    with LeadStore(database_path()) as store:
        result = store.list_prospects(
            search=search,
            source=source,
            stage=stage,
            tag=tag,
            min_score=min_score,
            live=live,
            starred=starred,
            excluded={"active": False, "excluded": True, "all": None}[exclusion],
            budget_ready=budget_ready,
            placement_ready=placement_ready,
            has_public_email=has_public_email,
            archetype=archetype,
            min_placement=min_placement,
            min_demand=min_demand,
            min_capacity=min_capacity,
            min_timing=min_timing,
            min_creative_capacity=min_creative_capacity,
            creative_review_ready=creative_review_ready,
            sort=sort,
            limit=100_000,
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "company",
            "website",
            "public_emails",
            "preferred_email",
            "placement_fit",
            "placement_confidence",
            "placement_ready",
            "specialist_roles",
            "agency_specialties",
            "stage",
            "excluded",
            "tags",
            "contact_count",
            "last_contacted_at",
            "next_follow_up_at",
            "contact_priority",
            "creative_capacity_score",
            "creative_capacity_review_ready",
            "distribution_investment",
            "recurring_creative_demand",
            "production_capacity_gap",
            "ai_brand_readiness",
            "timing_access",
            "budget_ready",
            "demand_score",
            "capacity_score",
            "timing_score",
            "sales_osint_score",
            "type",
            "latest_trigger",
            "source",
            "discovery_records",
            "notes",
        ]
    )
    for item in result["items"]:
        score = item["score"]
        outreach_emails = [
            contact["address"]
            for contact in item.get("contacts", {}).get("emails", [])
            if contact.get("outreach_eligible") and not contact.get("historical")
        ]
        writer.writerow(
            [
                _csv_cell(item["company_name"]),
                _csv_cell(item["company_url"]),
                _csv_cell("; ".join(outreach_emails)),
                _csv_cell((item.get("best_contact") or {}).get("address", "")),
                score["placement"]["score"],
                score["placement"]["confidence"],
                "yes" if score["placement"]["ready"] else "no",
                _csv_cell("; ".join(score["placement"]["specialist_roles"])),
                _csv_cell("; ".join(score["placement"]["agency_specialties"])),
                _csv_cell(item["stage"]),
                "yes" if item["excluded"] else "no",
                _csv_cell("; ".join(item["tags"])),
                item["contact_count"],
                item["last_contacted_at"] or "",
                item["next_follow_up_at"] or "",
                score["contact_priority"],
                score["creative_capacity"]["total"],
                "yes" if score["creative_capacity"]["review_ready"] else "no",
                score["creative_capacity"]["distribution_investment"],
                score["creative_capacity"]["recurring_creative_demand"],
                score["creative_capacity"]["production_capacity_gap"],
                score["creative_capacity"]["ai_brand_readiness"],
                score["creative_capacity"]["timing_access"],
                "yes" if score["budget_ready"] else "no",
                score["demand_score"],
                score["capacity_score"],
                score["timing_score"],
                item["web"]["sales"]["overall_score"],
                _csv_cell(item["archetype"]),
                _csv_cell(score["latest_trigger"]),
                _csv_cell(item["source_apex"]),
                _csv_cell("; ".join(item.get("discovery_evidence_urls", []))),
                _csv_cell(item["notes"]),
            ]
        )
    filename = f"beings-prospects-{datetime.now(UTC).date().isoformat()}.csv"
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/tags")
def tags() -> dict:
    with LeadStore(database_path()) as store:
        return {"items": store.list_tags()}


@app.get("/api/prospects/{candidate_id}")
def prospect(candidate_id: int) -> dict:
    with LeadStore(database_path()) as store:
        result = store.prospect_detail(candidate_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return result


@app.post("/api/prospects/{candidate_id}/scan", status_code=202)
async def scan_prospect(candidate_id: int) -> dict[str, object]:
    """Run one operator-requested company refresh, bypassing only its due dates."""

    _reject_new_work_during_smart_scan()
    with LeadStore(database_path()) as store:
        candidate = store.connection.execute(
            "SELECT hostname FROM candidates WHERE id=?",
            (int(candidate_id),),
        ).fetchone()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Prospect not found")
    hostname = str(candidate["hostname"] or "").strip().lower().rstrip(".")
    if not hostname:
        raise HTTPException(status_code=409, detail="Company has no scannable hostname")
    request = ScanRequest(
        sources=[],
        only_hosts=[hostname],
        max_hosts_per_source=1,
        max_network_inspections=1,
        concurrency=1,
        pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
        free_intelligence=True,
        max_sitemaps_per_site=4,
        max_common_crawl_lookups=1,
        max_news_lookups=1,
        max_frontier_scans=1,
        agency_discovery=False,
        ai=False,
        ai_web=False,
        max_ai_calls=0,
        max_ai_web_calls=0,
        manual_company_refresh=True,
    )
    result = await scan_manager.start(
        request,
        [],
        1,
        include_people_hydration=True,
    )
    return {**result, "candidate_id": candidate_id, "hostname": hostname, "mode": "company"}


@app.get("/api/prospects/{candidate_id}/similar")
def similar_prospects(
    candidate_id: int,
    limit: int = Query(default=12, ge=1, le=30),
) -> dict:
    with LeadStore(database_path()) as store:
        exists = store.connection.execute(
            "SELECT 1 FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Prospect not found")
        items = store.similar_prospects(candidate_id, limit=limit)
    return {"items": items, "total": len(items)}


@app.patch("/api/prospects/{candidate_id}/workflow")
def update_prospect_workflow(candidate_id: int, patch: WorkflowPatch) -> dict:
    if patch.stage is not None and patch.stage not in STAGES:
        raise HTTPException(status_code=422, detail="Unknown workflow stage")
    next_follow_up_set = "next_follow_up_at" in patch.model_fields_set
    with LeadStore(database_path()) as store:
        result = store.update_workflow(
            candidate_id,
            stage=patch.stage,
            starred=patch.starred,
            excluded=patch.excluded,
            scan_enabled=patch.scan_enabled,
            notes=patch.notes,
            tags=patch.tags,
            next_follow_up_at=(
                patch.next_follow_up_at.isoformat() if patch.next_follow_up_at else None
            ),
            update_next_follow_up=next_follow_up_set,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return result


@app.post("/api/prospects/{candidate_id}/activities", status_code=201)
def add_prospect_activity(candidate_id: int, activity: ActivityCreate) -> dict:
    with LeadStore(database_path()) as store:
        result = store.add_activity(
            candidate_id,
            activity_type=activity.activity_type,
            summary=activity.summary,
            contact_email=activity.contact_email,
            occurred_at=activity.occurred_at.isoformat() if activity.occurred_at else None,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return result


@app.get("/api/sources")
def sources() -> dict:
    sync_registry()
    with LeadStore(database_path()) as store:
        managed = store.list_sources()
        accepted = store.list_accepted_discovery_sources()
    discovery_payloads = [_discovery_source_payload(row) for row in accepted]
    return {
        "items": managed,
        "accepted_items": discovery_payloads,
        "discovery_items": [
            item for item in discovery_payloads if item["queue_visible"]
        ],
        "registry_path": str(REGISTRY_PATH),
    }


@app.post("/api/sources", status_code=201)
def add_source(source: SourceCreate) -> dict:
    try:
        apex = normalize_apex(source.apex)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    append_registry_source(REGISTRY_PATH, apex, category=source.category, notes=source.notes)
    with LeadStore(database_path()) as store:
        store.add_managed_source(
            apex,
            category=source.category,
            notes=source.notes,
            enabled=True,
            registry_managed=True,
        )
    return {"apex": apex, "registry_path": str(REGISTRY_PATH)}


@app.patch("/api/sources/{apex:path}")
def update_source(apex: str, patch: SourcePatch) -> dict:
    with LeadStore(database_path()) as store:
        updated = store.set_source_enabled(apex, patch.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"apex": apex, "enabled": patch.enabled}


def _discovery_source_payload(row: dict[str, Any]) -> dict[str, Any]:
    suggestion = _source_suggestion(row)
    method = _method_for_suggestion(suggestion)
    blockers = _source_activation_blockers(suggestion)
    latest_run = row.get("latest_run") if isinstance(row.get("latest_run"), dict) else None
    candidates_found = int(row.get("candidates_found") or 0)
    candidates_hydrated = int(row.get("candidates_hydrated") or 0)
    hydration_remaining = max(0, candidates_found - candidates_hydrated)
    source_result = _source_run_result(row, latest_run) if latest_run else None
    source_execution_state = str((source_result or {}).get("status") or "").lower()
    scan_policy = _source_scan_policy(row)
    if row.get("status") != "accepted":
        activation_state = "inactive"
    elif blockers:
        activation_state = "blocked"
    elif latest_run and latest_run.get("status") in {"queued", "running", "cancelling"}:
        activation_state = str(latest_run["status"])
    elif latest_run:
        latest_status = str(latest_run.get("status") or "complete").lower()
        if source_execution_state == "complete":
            activation_state = "hydration_due" if hydration_remaining else "complete"
        elif source_execution_state:
            activation_state = source_execution_state
        else:
            activation_state = latest_status
    else:
        activation_state = "not_scanned"
    label = str((_method_tab(method or "") or {}).get("label") or method or "source scan")
    activation_message = {
        "blocked": blockers[0] if blockers else "Source is blocked",
        "queued": f"Queued for {label}",
        "running": f"Scanning in {label}",
        "cancelling": f"Stopping {label}",
        "partial": (f"{candidates_found} companies found · {candidates_hydrated} hydrated"),
        "hydration_due": (
            f"Source complete · {hydration_remaining} companies still need hydration"
        ),
        "complete": (f"{candidates_found} companies found · {candidates_hydrated} hydrated"),
        "not_scanned": "Accepted and awaiting its first scan",
        "inactive": "Source is not accepted",
    }.get(activation_state, str((latest_run or {}).get("error") or activation_state))
    queue_visible = row.get("status") == "accepted" and activation_state not in {
        "complete",
        "completed",
    }
    return {
        **row,
        "execution_method": discovery_method_for(suggestion),
        "method": method,
        "source_execution_state": source_execution_state or activation_state,
        "source_result": source_result,
        "hydration_state": "due" if hydration_remaining else "complete",
        "hydration_remaining": hydration_remaining,
        "queue_visible": queue_visible,
        "runnable": (
            row.get("status") == "accepted"
            and not blockers
            and bool(scan_policy["scan_eligible"])
        ),
        **scan_policy,
        "blockers": blockers,
        "activation_state": activation_state,
        "activation": {
            "state": activation_state,
            "method": method,
            "run_id": (latest_run or {}).get("id"),
            "message": activation_message,
            "blockers": blockers,
        },
    }


def _active_discovery_runs() -> list[dict[str, object]]:
    active = discovery_method_manager.active_payloads()
    seen_ids = {int(run["run_id"]) for run in active if run.get("run_id") is not None}
    with LeadStore(database_path()) as store:
        persisted = store.list_source_execution_runs(limit=200)
    for run in persisted:
        if run.get("status") not in {"queued", "running", "cancelling"}:
            continue
        if int(run["id"]) in seen_ids:
            continue
        active.append({**run, "run_id": int(run["id"])})
    return active


@app.get("/api/discovery-methods")
def discovery_methods(compact: bool = False) -> dict[str, object]:
    with LeadStore(database_path()) as store:
        suggestion_rows = [] if compact else store.list_source_suggestions()
        accepted_rows = store.list_accepted_discovery_sources()
        accepted_by_id = {int(row["id"]): row for row in accepted_rows}
        accepted = [_discovery_source_payload(row) for row in accepted_rows]
        managed_sources = [] if compact else store.list_sources()
        execution = store.source_execution_payload(limit=20)
        scan_recency = store.discovery_scan_recency()
        reliability = store.discovery_method_reliability(
            [str(definition["id"]) for definition in DISCOVERY_METHOD_TABS]
        )
    tabs = []
    for definition in DISCOVERY_METHOD_TABS:
        source_kinds = set(definition.get("source_kinds") or [])
        requirements = list(definition.get("requires") or [])
        missing = [name for name in requirements if not os.getenv(str(name))]
        if definition["id"] == "web_ct":
            sources = [source for source in managed_sources if source.get("enabled")]
        elif definition["id"] == "find_sources":
            sources = [
                _discovery_source_payload(accepted_by_id.get(int(row["id"]), row))
                for row in suggestion_rows
            ]
        else:
            sources = [source for source in accepted if source.get("source_kind") in source_kinds]
        tabs.append(
            {
                **definition,
                "sources": sources,
                "scanners": (
                    _frontier_scanner_payloads()
                    if definition["id"] == "company_intelligence"
                    else []
                ),
                "feed_sources": [],
                "ready": not missing,
                "missing": missing,
                "last_scan_at": scan_recency.get(str(definition["id"])),
                "reliability": reliability[str(definition["id"])],
            }
        )
    active_runs = _active_discovery_runs()
    persisted_active = execution.get("active")
    if not active_runs and persisted_active:
        active_runs = [persisted_active]
    return {
        "tabs": tabs,
        "capabilities": {
            "public_web": True,
            "openai": bool(os.getenv(OPENAI_SECRET)),
            "apify": bool(os.getenv(APIFY_SECRET)),
        },
        "active_run": active_runs[0] if active_runs else None,
        "active_runs": active_runs,
        "recent_runs": execution.get("runs", []),
    }


@app.post("/api/discovery-methods/estimates")
def estimate_discovery_method(request: DiscoveryMethodRunRequest) -> dict[str, object]:
    omitted_sources = _reconcile_source_request(request)
    suggestions = discovery_method_manager._selected_suggestions(request)
    openai_ready = bool(os.getenv(OPENAI_SECRET))
    blockers = [
        f"{suggestion.name}: {reason}"
        for suggestion in suggestions
        for reason in _source_activation_blockers(suggestion)
    ]
    if (request.people_hydration_enabled and "people_hydration_enabled" in request.model_fields_set
            and resolve_treg_credentials() is None):
        blockers.append("Configure Treg in Settings for People hydration")
    if (request.microlink_enabled and "microlink_enabled" in request.model_fields_set
            and not os.getenv(MICROLINK_SECRET)):
        blockers.append("Configure Microlink in Settings for paid fallback")
    if request.method == "searches" and not openai_ready:
        blockers.append("Add an OpenAI API key in Settings to run repeatable searches")
    source_count = _discovery_source_total(request, suggestions)
    microlink_ready = request.microlink_enabled and bool(os.getenv(MICROLINK_SECRET))
    uses_source_crawl = request.method in SOURCE_CRAWL_DISCOVERY_METHODS and source_count > 0
    max_microlink_source_pages = (
        source_count * min(request.max_pages, SOURCE_MICROLINK_MAX_PAGES)
        if microlink_ready and uses_source_crawl
        else 0
    )
    max_microlink_website_fallbacks = (
        request.max_network_inspections
        if microlink_ready and (request.hydrate_companies or request.method == "public_graph")
        else 0
    )
    source_resolution = (
        openai_ready
        and request.method not in SERVER_OWNED_DISCOVERY_METHODS
        and (request.method == "searches" or request.ai_resolution)
    )
    candidate_review = (
        request.method != "company_intelligence"
        and openai_ready
        and request.max_ai_reviews > 0
    )
    uses_openai = source_resolution or candidate_review
    people_company_ceiling = 0
    if (request.people_hydration_enabled
            and (request.hydrate_companies or request.method == "public_graph")
            and resolve_treg_credentials() is not None):
        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            coverage = store.treg_people_hydration_status(
                limit=TREG_PEOPLE_MAX_COHORT_SIZE, coverage_only=True,
            )
        people_company_ceiling = min(request.max_network_inspections, coverage["due_companies"])
    people_estimate = estimate_people_hydration(
        company_count=people_company_ceiling,
        people_limit=DEFAULT_PEOPLE_LIMIT,
        email_limit=0,
        email_max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
    )
    return {
        "estimate_id": uuid.uuid4().hex,
        "estimated_at": datetime.now(UTC).isoformat(),
        "method": request.method,
        "source_count": source_count,
        "omitted_source_count": len(omitted_sources),
        "omitted_sources": omitted_sources,
        "suggestion_ids": request.suggestion_ids,
        "scanner_ids": request.scanner_ids,
        "feed_source_ids": request.feed_source_ids,
        "lookback_days": request.lookback_days,
        "max_feed_items": request.max_feed_items,
        "max_pages": request.max_pages,
        "max_page_requests": source_count * request.max_pages,
        "max_candidates": request.max_candidates,
        "max_network_inspections": request.max_network_inspections,
        "max_ai_tool_calls": request.max_ai_tool_calls,
        "provider_calls": {
            "openai_source_resolutions": source_count if source_resolution else 0,
            "openai_source_web_calls": (
                source_count * request.max_ai_tool_calls if source_resolution else 0
            ),
            "openai_candidate_reviews": (request.max_ai_reviews if candidate_review else 0),
            "openai_web_reviews": (request.max_ai_web_reviews if candidate_review else 0),
            "microlink_source_pages": max_microlink_source_pages,
            "microlink_website_fallbacks": max_microlink_website_fallbacks,
            "treg_people_searches": people_estimate["search_request_count"],
            "treg_email_attempts": people_estimate["email_request_count"],
            "apify": 0,
        },
        "provider_cost_usd": (
            None
            if uses_openai or max_microlink_source_pages or max_microlink_website_fallbacks
            else people_estimate["maximum_usd"]
        ),
        "known_provider_cost_usd": people_estimate["maximum_usd"],
        "has_unpriced_provider_usage": bool(
            uses_openai or max_microlink_source_pages or max_microlink_website_fallbacks
        ),
        "people_hydration": {
            **people_estimate,
            "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        },
        "cost_note": (
            "Direct public retrieval runs first. Eligible failures may use Microlink within "
            "the displayed page cap; OpenAI runs within its displayed call caps. No Apify work."
            if uses_openai and max_microlink_source_pages
            else "Direct public retrieval runs first; eligible failures may use Microlink "
            "within the displayed page cap. No Apify work."
            if max_microlink_source_pages
            else "Uses your OpenAI account within the displayed call caps; no Apify work."
            if uses_openai
            else "Public retrieval is free."
        )
        + (
            f" Ranked companies touched by this scan also include Treg people hydration "
            f"for at most {people_company_ceiling} due companies, capped at "
            f"${float(people_estimate['maximum_usd']):.6f}."
            if people_company_ceiling
            else " People hydration is off." if not request.people_hydration_enabled
            else " No ranked touched companies currently require Treg people hydration."
        ),
        "english_only": True,
        "runnable": not blockers and (
            request.method in SERVER_OWNED_DISCOVERY_METHODS or source_count > 0
        ),
        "blockers": blockers,
        "request": request.model_dump(mode="json"),
    }


@app.post("/api/discovery-methods/runs", status_code=202)
async def start_discovery_method(request: DiscoveryMethodRunRequest) -> dict[str, object]:
    _reject_new_work_during_smart_scan()
    return await discovery_method_manager.start(request)


@app.get("/api/discovery-methods/status")
def discovery_method_status() -> dict[str, object]:
    active_runs = _active_discovery_runs()
    if active_runs:
        return {
            **active_runs[0],
            "active_run": active_runs[0],
            "active_runs": active_runs,
        }
    if discovery_method_manager.run_id is not None:
        payload = discovery_method_manager.status_payload()
        return {**payload, "active_run": None, "active_runs": []}
    with LeadStore(database_path()) as store:
        payload = store.source_execution_payload(limit=1)
    latest = (payload.get("runs") or [None])[0]
    return {
        **(latest or {"status": "idle", "message": "Ready"}),
        "active_run": None,
        "active_runs": [],
    }


@app.get("/api/intelligence")
def intelligence() -> dict:
    with LeadStore(database_path()) as store:
        payload = store.intelligence_payload()
    payload["scout_status"] = source_scout_manager.state
    payload["scout_defaults"] = {
        "max_suggestions": DEFAULT_SCOUT_SUGGESTIONS,
        "max_tool_calls": DEFAULT_SCOUT_TOOL_CALLS,
        "keyword_combinations": DEFAULT_SCOUT_KEYWORD_COMBINATIONS,
        "timeout_seconds": SOURCE_SCOUT_TIMEOUT_SECONDS,
    }
    payload["source_scout_available"] = bool(os.getenv("OPENAI_API_KEY"))
    return payload


@app.get("/api/research")
def research() -> dict:
    with LeadStore(database_path()) as store:
        return build_research_brief(store.list_sources())


@app.get("/api/source-scout-status")
def source_scout_status() -> dict:
    return source_scout_manager.state


@app.post("/api/source-scouts", status_code=202)
async def start_source_scout(request: SourceScoutRequest) -> dict:
    _reject_new_work_during_smart_scan()
    return await source_scout_manager.start(request)


def _persist_source_review(suggestion_id: int, patch: SuggestionPatch):
    with LeadStore(database_path()) as store:
        canonical_id = store.resolve_source_suggestion_id(suggestion_id)
        previous = next(
            (
                row
                for row in store.list_source_suggestions(
                    suggestion_ids=[canonical_id] if canonical_id is not None else []
                )
                if canonical_id is not None and int(row["id"]) == canonical_id
            ),
            None,
        )
        suggestion = store.update_source_suggestion_status(suggestion_id, patch.status)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    suggestion_model = _source_suggestion(suggestion)
    if (
        patch.status == "accepted"
        and suggestion.get("source_kind") == "tenant_apex"
        and suggestion.get("apex")
        and not source_execution_blockers(suggestion_model)
    ):
        apex = normalize_apex(str(suggestion["apex"]))
        category = str(suggestion["category"])
        if category == "Brand group":
            category = "Ecommerce"
        notes = str(suggestion["discovery_mechanism"])
        append_registry_source(REGISTRY_PATH, apex, category=category, notes=notes)
        with LeadStore(database_path()) as store:
            store.add_managed_source(
                apex,
                category=category,
                tier="Research",
                notes=notes,
                enabled=False,
                registry_managed=True,
            )
    return previous, suggestion


def _source_review_catalogue(suggestion_id: int, fallback=None):
    with LeadStore(database_path()) as store:
        rows = store.list_accepted_discovery_sources(suggestion_id=suggestion_id)
        return rows[0] if rows else fallback


@app.patch("/api/intelligence/suggestions/{suggestion_id}")
async def update_suggestion(suggestion_id: int, patch: SuggestionPatch) -> dict:
    if patch.status == "accepted" and patch.activate:
        _reject_new_work_during_smart_scan()
    previous, suggestion = await asyncio.to_thread(_persist_source_review, suggestion_id, patch)
    cancelled_run_ids: list[int] = []
    if patch.status != "accepted":
        cancellation_reason = (
            "source_rejected" if patch.status == "rejected" else "source_not_accepted"
        )
        with LeadStore(database_path()) as store:
            store.cancel_queued_source_execution_runs(
                suggestion_id,
                reason=cancellation_reason,
            )
        cancelled_run_ids = await discovery_method_manager.cancel_source(
            suggestion_id,
            reason=cancellation_reason,
        )

    catalogue_row = await asyncio.to_thread(
        _source_review_catalogue, int(suggestion["id"]), suggestion
    )
    source = _discovery_source_payload(catalogue_row)
    activation = {
        "state": "not_requested",
        "method": source.get("method"),
        "run_id": None,
        "message": "Source saved without starting a scan",
        "blockers": source.get("blockers", []),
    }
    if patch.status == "accepted" and patch.activate:
        blockers = list(source.get("blockers") or [])
        if blockers:
            activation = {
                **activation,
                "state": "blocked",
                "message": blockers[0],
            }
        else:
            latest = source.get("latest_run") or {}
            latest_status = str(latest.get("status") or "")
            prior_accepted = bool(previous and previous.get("status") == "accepted")
            candidates_found = int(source.get("candidates_found") or 0)
            hydrated_inventory = (
                latest_status in {"complete", "partial", "failed"}
                and candidates_found > 0
                and candidates_found <= int(source.get("candidates_hydrated") or 0)
            )
            if latest_status in {"queued", "running", "cancelling"}:
                activation = {
                    **activation,
                    "state": "already_active",
                    "run_id": latest.get("id"),
                    "message": str(
                        source.get("activation", {}).get("message")
                        or "Source scan is already active"
                    ),
                }
            elif prior_accepted and hydrated_inventory:
                activation = {
                    **activation,
                    "state": "already_active",
                    "run_id": latest.get("id"),
                    "message": (
                        "Source inventory is already hydrated; use an explicit run to refresh it"
                    ),
                }
            else:
                request = _automatic_source_request(
                    int(suggestion["id"]),
                    _source_suggestion(catalogue_row),
                )
                queued = await discovery_method_manager.enqueue(request)
                activation = {
                    **activation,
                    "state": str(queued.get("activation_state") or "queued"),
                    "run_id": queued.get("id") or queued.get("run_id"),
                    "message": str(queued.get("message") or "Source scan queued"),
                }
        refreshed = await asyncio.to_thread(_source_review_catalogue, int(suggestion["id"]))
        if refreshed is not None:
            source = _discovery_source_payload(refreshed)
    return {
        "id": int(suggestion["id"]),
        "requested_id": suggestion_id,
        "status": patch.status,
        "apex": suggestion.get("apex"),
        "source": source,
        "execution_method": source.get("execution_method"),
        "method": source.get("method"),
        "activation": activation,
        "cancelled_run_ids": cancelled_run_ids,
    }


@app.post("/api/intelligence/suggestions/{suggestion_id}/hydrate", status_code=202)
async def hydrate_source_backlog(
    suggestion_id: int,
    request: SourceHydrationSupplementRequest,
) -> dict[str, Any]:
    """Hydrate unresolved source-linked companies without fetching the source again."""

    _reject_new_work_during_smart_scan()
    with LeadStore(database_path()) as store:
        canonical_id = store.resolve_source_suggestion_id(suggestion_id)
        source = next(
            (
                row
                for row in store.list_source_suggestions()
                if canonical_id is not None and int(row["id"]) == canonical_id
            ),
            None,
        )
        backlog = (
            store.source_hydration_backlog(
                canonical_id,
                limit=request.max_network_inspections,
            )
            if canonical_id is not None
            else []
        )
    if source is None:
        raise HTTPException(status_code=404, detail="Source suggestion not found")
    if source.get("status") != "accepted":
        raise HTTPException(
            status_code=409,
            detail="Accept the source before hydrating its backlog",
        )
    if not backlog:
        return {
            "status": "complete",
            "phase": "complete",
            "message": "Source-linked companies are already hydrated",
            "suggestion_id": canonical_id,
            "hosts_requested": 0,
            "source_refetched": False,
        }

    hosts = [str(item["hostname"]) for item in backlog]
    network_cap = min(request.max_network_inspections, len(hosts))
    scan_request = ScanRequest(
        sources=[],
        only_hosts=hosts,
        max_hosts_per_source=max(1, len(hosts)),
        max_network_inspections=network_cap,
        adaptive_refresh=True,
        concurrency=min(20, max(1, network_cap)),
        pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
        english_only=True,
        free_intelligence=True,
        max_sitemaps_per_site=4,
        max_common_crawl_lookups=min(20, network_cap),
        max_news_lookups=min(5, network_cap),
        max_frontier_scans=min(20, network_cap),
        ai=True,
        ai_web=request.max_ai_web_calls > 0,
        max_ai_calls=min(request.max_ai_calls, len(hosts)),
        max_ai_web_calls=min(request.max_ai_web_calls, request.max_ai_calls, len(hosts)),
        evergreen_discovery=False,
    )
    started = await scan_manager.start(
        scan_request,
        [],
        len(hosts),
        include_people_hydration=True,
    )
    return {
        **started,
        "suggestion_id": canonical_id,
        "hosts_requested": len(hosts),
        "source_refetched": False,
    }


@app.get("/api/runs")
def runs(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    with LeadStore(database_path()) as store:
        return {"items": store.list_runs(limit=limit)}


@app.post("/api/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: int) -> dict:
    _reject_new_work_during_smart_scan()
    with LeadStore(database_path()) as store:
        metadata = store.run_metadata(run_id)
        if not metadata:
            raise HTTPException(status_code=404, detail="Run not found")
        if metadata["status"] not in {"failed", "partial"}:
            raise HTTPException(
                status_code=409,
                detail="Only failed or partial runs can be resumed",
            )
        config = dict(metadata.get("config") or {})
        sources = list(config.get("apexes") or [])
        if not sources:
            raise HTTPException(status_code=422, detail="Run has no resumable sources")
        summary = dict(metadata.get("summary") or {})
        failed_hosts = list(
            dict.fromkeys(summary.get("retryable_hosts") or config.get("only_hosts") or [])
        )
        if not failed_hosts:
            raise HTTPException(
                status_code=409,
                detail="Run has no recorded host-level failures to resume",
            )
        config["only_hosts"] = failed_hosts
        try:
            request = ScanRequest.model_validate(config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Run configuration is invalid") from exc
        max_candidates = len(failed_hosts)
    result = await scan_manager.start(
        request,
        sources,
        max_candidates,
        force_refresh=True,
        include_people_hydration=request.people_hydration_enabled,
    )
    scan_manager.state["resumed_from"] = run_id
    logger.info(
        "resumed scan from run_id=%s retryable_hosts=%d",
        run_id,
        len(failed_hosts),
    )
    return {
        **result,
        "resumed_from": run_id,
        "retrying_count": len(failed_hosts),
    }


@app.get("/api/scan-status")
def scan_status(job_id: str | None = Query(default=None, max_length=64)) -> dict:
    status = scan_manager.status_for(job_id)
    if scan_manager.running:
        with LeadStore(database_path()) as store:
            progress = store.active_run_progress(status.get("job_id"))
        if progress:
            status.update(progress)
    # Read only in-memory telemetry; global status must not hydrate inventories or start work.
    fields = {"id", "run_id", "status", "phase", "phase_index", "progress_percent",
              "started_at", "updated_at", "finished_at", "error_total", "phase_errors",
              "resumed_after_restart_at", "summary", "method", "method_label", "work",
              "new_companies_added", "source_progress"}

    def compact(payload: dict) -> dict:
        return {key: value for key, value in payload.items() if key in fields}

    status["scan_overview"] = {
        "smart": {**compact(smart_scan_manager.state), "phases": list(SMART_SCAN_PHASES)},
        "methods": [compact(run) for run in discovery_method_manager.active_payloads()],
        "paid": compact(ad_discovery_manager.state),
        "scout": compact(source_scout_manager.state),
        "people": {
            **compact(people_hydration_manager.state),
            **{key: people_hydration_manager.state[key] for key in
               ("message", "company_count", "processed_companies", "current_company")
               if key in people_hydration_manager.state},
        },
    }
    return status


@app.post("/api/web-scan/estimates")
def estimate_web_scan(request: WebScanEstimateRequest) -> dict:
    """Describe one deterministic web scan without starting network work."""
    if request.people_hydration_enabled and resolve_treg_credentials() is None:
        raise HTTPException(422, "Configure Treg in Settings for People hydration")
    if request.microlink_enabled and not os.getenv(MICROLINK_SECRET):
        raise HTTPException(422, "Configure Microlink in Settings for paid fallback")
    with LeadStore(database_path()) as store:
        sources = store.enabled_sources()
        candidate_capacity = store.scan_capacity(
            sources,
            request.max_hosts_per_source,
        )
        people_company_ceiling = 0
        if request.people_hydration_enabled:
            coverage = store.treg_people_hydration_status(
                limit=TREG_PEOPLE_MAX_COHORT_SIZE, coverage_only=True,
            )
            people_company_ceiling = min(
                request.max_network_inspections, coverage["due_companies"],
            )
    people_estimate = estimate_people_hydration(
        company_count=people_company_ceiling,
        people_limit=DEFAULT_PEOPLE_LIMIT,
        email_limit=0,
        email_max_cost_usd=DEFAULT_EMAIL_MAX_COST_USD,
    )
    scan_request = ScanRequest(
        sources=sources,
        people_hydration_enabled=request.people_hydration_enabled,
        microlink_enabled=request.microlink_enabled,
        max_hosts_per_source=request.max_hosts_per_source,
        max_network_inspections=request.max_network_inspections,
        adaptive_refresh=True,
        concurrency=20,
        pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
        english_only=True,
        free_intelligence=request.free_intelligence,
        max_sitemaps_per_site=4,
        max_common_crawl_lookups=min(20, request.max_network_inspections),
        max_news_lookups=min(5, request.max_network_inspections),
        agency_discovery=request.agency_discovery,
        ai=False,
        ai_web=False,
        max_ai_calls=0,
        max_ai_web_calls=0,
        evergreen_discovery=False,
    )
    return {
        "estimate_id": uuid.uuid4().hex,
        "estimated_at": datetime.now(UTC).isoformat(),
        "enabled_sources": sources,
        "enabled_source_count": len(sources),
        "candidate_capacity": candidate_capacity,
        "max_hosts_per_source": request.max_hosts_per_source,
        "max_network_inspections": request.max_network_inspections,
        "max_page_requests": request.max_network_inspections * scan_request.pages_per_site,
        "provider_cost_usd": (
            None if request.microlink_enabled and os.getenv(MICROLINK_SECRET)
            else people_estimate["maximum_usd"]
        ),
        "known_provider_cost_usd": people_estimate["maximum_usd"],
        "has_unpriced_provider_usage": bool(
            request.microlink_enabled and os.getenv(MICROLINK_SECRET)
        ),
        "provider_calls": {
            "apify": 0,
            "openai": 0,
            "microlink_website_fallbacks": (
                request.max_network_inspections
                if request.microlink_enabled and os.getenv(MICROLINK_SECRET) else 0
            ),
            "treg_people_searches": people_estimate["search_request_count"],
            "treg_email_attempts": people_estimate["email_request_count"],
        },
        "people_hydration": {
            **people_estimate,
            "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        },
        "english_only": True,
        "request": scan_request.model_dump(mode="json"),
    }


@app.post("/api/scans/import", status_code=202)
async def import_and_scan(request: ImportScanRequest) -> dict:
    async with import_scan_lock:
        _reject_new_work_during_smart_scan(allow_cleanup=True)
        saving_only = getattr(smart_scan_manager, "cleaning_up", False)
        try:
            result = await asyncio.to_thread(
                save_company_urls, database_path(), request.urls, request.tag
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        hosts = result.pop("hosts")
        if not hosts:
            return {**result, "status": "not_started", "message": "No eligible company URLs"}
        if saving_only or getattr(smart_scan_manager, "cleaning_up", False):
            return {
                **result, "status": "not_started",
                "message": "Companies saved. Re-import after cleanup to scan their websites.",
            }
        # The database worker yielded: recheck workflow ownership before launching.
        try:
            _reject_new_work_during_smart_scan()
            scan = await scan_manager.start(
                ScanRequest(
                    sources=[], only_hosts=hosts, max_hosts_per_source=len(hosts),
                    max_network_inspections=len(hosts), people_hydration_enabled=False,
                    microlink_enabled=False, max_frontier_scans=min(len(hosts), 500),
                ),
                [], len(hosts), include_people_hydration=False,
            )
        except Exception as exc:
            logger.exception("Imported companies saved but scan launch failed")
            return {**result, "status": "not_started",
                    "message": "Companies saved; scan did not start. Re-import to retry.",
                    "error": str(getattr(exc, "detail", exc))}
        return {**result, **scan}


class TagScanRequest(BaseModel):
    tag: str = Field(min_length=1, max_length=40)
    mission_id: str = Field(default="beings", min_length=1, max_length=100)


class MissionWebsiteScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission_id: str = Field(min_length=1, max_length=100)
    company_count: int = Field(default=25, ge=1, le=100)


@app.post("/api/scans/mission", status_code=202)
async def scan_selected_mission(request: MissionWebsiteScanRequest) -> dict:
    from .missions.research import website_cohort

    _reject_new_work_during_smart_scan()

    def resolve():
        with LeadStore(database_path(), initialize=False, read_only=True) as store:
            store.connection.execute("BEGIN")
            return website_cohort(
                store.connection, request.mission_id, datetime.now(UTC), request.company_count,
            )

    try:
        hosts = await asyncio.to_thread(resolve)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not hosts:
        return {"status": "not_started", "companies": 0, "mission_id": request.mission_id}
    _reject_new_work_during_smart_scan()
    result = await scan_manager.start(
        ScanRequest(
            sources=[], only_hosts=hosts, max_hosts_per_source=len(hosts),
            max_network_inspections=len(hosts), max_frontier_scans=len(hosts),
            refresh_mission_id=request.mission_id,
            people_hydration_enabled=False, microlink_enabled=False, ai=False, ai_web=False,
            max_ai_calls=0, max_ai_web_calls=0,
        ),
        [], len(hosts), include_people_hydration=False,
    )
    return {**result, "companies": len(hosts), "mission_id": request.mission_id}


@app.post("/api/scans/tag", status_code=202)
async def scan_selected_tag(request: TagScanRequest) -> dict:
    from .company_tags import normalize_tag, tagged_candidates
    from .missions.store import get_mission

    _reject_new_work_during_smart_scan()

    def resolve():
        with LeadStore(database_path()) as store:
            get_mission(store.connection,
                        "beings" if request.mission_id == "all" else request.mission_id)
            rows = tagged_candidates(store, request.tag)
            return list(dict.fromkeys(r["hostname"] for r in rows)), [r["id"] for r in rows]

    try:
        hosts, candidate_ids = await asyncio.to_thread(resolve)
        tag = normalize_tag(request.tag)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(hosts) > 10_000:
        raise HTTPException(422, "Tag exceeds the 10,000-company scan limit")
    _reject_new_work_during_smart_scan()
    result = await scan_manager.start(
        ScanRequest(
            sources=[], only_hosts=hosts, max_hosts_per_source=len(hosts),
            max_network_inspections=len(hosts), manual_tag_refresh=True,
            refresh_tag=tag, refresh_mission_id=request.mission_id,
            refresh_candidate_ids=candidate_ids,
            people_hydration_enabled=False, microlink_enabled=False,
            max_frontier_scans=min(len(hosts), 500), ai=False, ai_web=False,
            max_ai_calls=0, max_ai_web_calls=0,
        ), [], len(hosts), include_people_hydration=False,
    )
    return {**result, "tag": tag, "companies": len(hosts)}


@app.post("/api/scans", status_code=202)
async def start_scan(request: ScanRequest) -> dict:
    _reject_new_work_during_smart_scan()
    if request.manual_company_refresh or request.manual_tag_refresh:
        raise HTTPException(
            status_code=422,
            detail="Manual company refresh is available only from an individual company",
        )
    if request.ai and request.max_ai_web_calls > request.max_ai_calls:
        raise HTTPException(
            status_code=422,
            detail="AI web calls cannot exceed total AI calls",
        )
    with LeadStore(database_path()) as store:
        available = set(store.enabled_sources())
    selected = request.sources if request.sources is not None else sorted(available)
    invalid = sorted(set(selected) - available)
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Sources are disabled or unknown: {', '.join(invalid)}",
        )
    with LeadStore(database_path()) as store:
        max_candidates = store.scan_capacity(selected, request.max_hosts_per_source)
    return await scan_manager.start(
        request,
        selected,
        max_candidates,
        include_people_hydration=request.people_hydration_enabled,
    )


@app.post("/api/mega-scan", status_code=202)
async def start_mega_scan() -> dict:
    """Start the single dashboard scan using capability-aware, bounded defaults."""
    _reject_new_work_during_smart_scan()
    if scan_manager.running:
        raise HTTPException(status_code=409, detail="A scan is already running")
    ai_available = bool(os.getenv("OPENAI_API_KEY"))
    with LeadStore(database_path()) as store:
        sources = store.enabled_sources()
        max_candidates = store.scan_capacity(sources, MEGA_MAX_HOSTS_PER_SOURCE)
        scout_due = store.source_scout_due()
    request = ScanRequest(
        max_hosts_per_source=MEGA_MAX_HOSTS_PER_SOURCE,
        max_network_inspections=MEGA_MAX_NETWORK_INSPECTIONS,
        adaptive_refresh=True,
        concurrency=30,
        pages_per_site=DEFAULT_SITE_RESEARCH_PAGES,
        free_intelligence=True,
        max_sitemaps_per_site=4,
        max_common_crawl_lookups=40,
        max_news_lookups=8,
        agency_discovery=True,
        ai=ai_available,
        ai_web=ai_available,
        min_ai_score=45,
        max_ai_calls=MEGA_MAX_AI_CALLS,
        max_ai_web_calls=MEGA_MAX_AI_WEB_CALLS,
        evergreen_discovery=ai_available and scout_due,
        evergreen_max_suggestions=DEFAULT_SCOUT_SUGGESTIONS,
        evergreen_max_tool_calls=DEFAULT_SCOUT_TOOL_CALLS,
    )
    result = await scan_manager.start(
        request,
        sources,
        max_candidates,
        include_people_hydration=True,
    )
    result["mode"] = "mega"
    return result


def record_spending_event(*, cost: Decimal, operation: str) -> None:
    from .spending import micro, record_event

    try:
        with LeadStore(database_path()) as store:
            record_event(store.connection, reference="seo:" + uuid.uuid4().hex,
                         service="dataforseo", amount=micro(cost), operation=operation)
            store.connection.commit()
    except sqlite3.Error:
        # The existing parent run total remains the authoritative fallback.
        logger.warning("Could not persist a dated SEO cost receipt")


@app.get("/api/spend")
def spending_summary(period: Literal["all", "month", "30d"] = "all") -> dict:
    from .spending import summary

    with LeadStore(database_path(), initialize=False, read_only=True) as store:
        store.connection.execute("BEGIN")
        return summary(store.connection, period)


_spending_sync_lock = asyncio.Lock()


@app.post("/api/spend/sync")
async def sync_spending_history() -> dict:
    from .spending import collect_reports, save_reports

    if _spending_sync_lock.locked():
        raise HTTPException(status_code=409, detail="Billing history refresh is already running")
    async with _spending_sync_lock:
        def earliest_scan():
            with LeadStore(database_path(), initialize=False, read_only=True) as store:
                row = store.connection.execute("SELECT MIN(started_at) FROM runs").fetchone()
                return row[0] or datetime.now(UTC).isoformat()

        earliest = await asyncio.to_thread(earliest_scan)
        reports = await collect_reports(earliest=earliest)

        def publish():
            with LeadStore(database_path()) as store:
                save_reports(store.connection, reports)

        await asyncio.to_thread(publish)
        return await asyncio.to_thread(spending_summary)


@app.get("/api/smart-scan/paid-estimate")
async def smart_scan_paid_estimate(
    request: Request,
    ai_sites: int = Query(default=0, ge=0, le=SMART_SCAN_MAX_CANDIDATE_AI_CALLS),
    seo_sites: int = Query(default=0, ge=0, le=SMART_SCAN_DATAFORSEO_MAX_TARGET_LIMIT),
    due_hydration: bool = Query(default=True),
    source_ai: bool | None = Query(default=None),
    microlink: bool = Query(default=False),
    people_sites: int = Query(default=0, ge=0, le=TREG_PEOPLE_MAX_COHORT_SIZE),
    ads_sites: int = Query(default=0, ge=0, le=500),
    ads_max_usd: Decimal = Query(default=Decimal("5.00"), ge=Decimal("0.10"),  # noqa: B008
                                le=Decimal("100.00"), decimal_places=2),  # noqa: B008
) -> dict:
    _require_loopback(request)
    return await asyncio.to_thread(
        _smart_scan_paid_estimate, ai_sites, seo_sites, due_hydration, source_ai, microlink,
        **({"people_sites": people_sites} if people_sites else {}),
        **({"ads_sites": ads_sites, "ads_max_usd": ads_max_usd} if ads_sites else {}),
    )


def _inventory_cache_scope(database: Path, stat: os.stat_result, policy) -> str | None:
    with suppress(OSError, ValueError):
        identity = (socket.gethostname(), str(database), stat.st_dev, stat.st_ino,
                    getattr(stat, "st_birthtime", None), policy, disallowed_domains())
        return hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
    return None


@app.get("/api/smart-scan")
def smart_scan_status(fast: bool = False) -> dict[str, object]:
    """Return the fixed safety profile, current backlog, and bounded live activity."""

    if fast:
        payload = smart_scan_manager.status_payload(fast=True)
        if payload.get("inventory_pending") and not payload.get("preparing"):
            with suppress(OSError):
                database = database_path().resolve()
                payload["inventory_cache_scope"] = _inventory_cache_scope(
                    database, database.stat(),
                    smart_scan_manager.state.get("max_company_failure_rate"),
                )
        return payload
    return smart_scan_manager.status_payload(refresh_inventory=not smart_scan_manager.running)


smart_scan_inventory_check = InventoryCheck()


def _read_smart_scan_inventory_check(policy, cancel, progress):
    token = _smart_scan_inventory_cancel.set(cancel)
    try:
        as_of = datetime.now(UTC).isoformat()
        inventory = _prepare_smart_scan_inventory(policy, progress)
        return {"inventory": inventory, "inventory_as_of": as_of}
    finally:
        _smart_scan_inventory_cancel.reset(token)


@app.get("/api/smart-scan/inventory")
async def smart_scan_inventory_status(check_id: str | None = None) -> dict[str, object]:
    manager = smart_scan_manager
    payload = manager.status_payload(fast=True)
    if manager.running or manager.preparation is not None:
        await smart_scan_inventory_check.shutdown()
        return manager.status_payload(fast=True)
    database = database_path().resolve()
    policy = manager.state.get("max_company_failure_rate")
    stat = database.stat()
    scope = _inventory_cache_scope(database, stat, policy)
    key = (str(database), stat.st_dev, stat.st_ino, policy, scope,
           payload.get("id"), payload.get("status"), payload.get("updated_at"))
    result = await smart_scan_inventory_check.poll(
        key, check_id, partial(_read_smart_scan_inventory_check, policy),
    )
    # Control requests may change the run while an older read is being drained.
    current = manager.status_payload(fast=True)
    if any(current.get(field) != payload.get(field) for field in ("id", "status", "updated_at")):
        return current
    return {**current, **result, "inventory_cache_scope": scope}


@app.post("/api/smart-scan", status_code=202)
async def start_smart_scan(
    request: SmartScanStartRequest | None = None,
) -> dict[str, object]:
    """Start exhaustive free/public work with an optional bounded AI phase."""

    if _company_purge_active():
        raise HTTPException(409, "Wait for the company data purge to finish")
    if microlink_scan.active or microlink_contacts.active:
        raise HTTPException(409, "Wait for the active Microlink scan to finish")
    return await smart_scan_manager.start(request or SmartScanStartRequest())


@app.post("/api/smart-scan/pause")
async def pause_smart_scan() -> dict[str, object]:
    """Persist pause intent immediately and interrupt the active child safely."""

    return await smart_scan_manager.pause()


@app.post("/api/smart-scan/resume")
async def resume_smart_scan() -> dict[str, object]:
    """Resume a paused Smart Scan without discarding completed child work."""

    if _company_purge_active():
        raise HTTPException(409, "Wait for the company data purge to finish")
    return await smart_scan_manager.resume()


@app.post("/api/smart-scan/stop")
async def stop_smart_scan() -> dict[str, object]:
    """Record terminal stop immediately and preserve already committed child work."""

    return await smart_scan_manager.stop()


@app.post("/api/smart-scan/clear")
async def clear_smart_scan() -> dict[str, object]:
    """Dismiss terminal progress and logs while preserving research and history."""

    return await smart_scan_manager.clear()


@app.get("/api/smart-scan/errors")
async def smart_scan_errors(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    """Page through the complete durable error history for the current Smart Scan."""

    return await smart_scan_manager.refresh_error_presentation(offset=offset, limit=limit)


class BrowserServer(uvicorn.Server):
    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            port = self.servers[0].sockets[0].getsockname()[1]
            address = f"http://{self.config.host}:{port}/"
            logging.getLogger("uvicorn.error").info("Dashboard ready at %s", address)
            Thread(target=webbrowser.open, args=(address,), daemon=True).start()


def launch_in_browser() -> None:
    main(open_browser=True)


def main(*, open_browser: bool = False) -> None:
    from .oracle_deployment import assert_local_store_allowed
    from .release_mode import requires_user_mission

    host = os.getenv("BEINGS_LEADS_HOST", "127.0.0.1")
    if (open_browser or requires_user_mission()) and host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The dashboard requires a loopback bind. Use 127.0.0.1 or localhost.")
    assert_local_store_allowed(database_path())
    if os.getenv("BEINGS_LEADS_FILE_LOG", "0" if open_browser else "1") == "1":
        from .error_logging import configure_runtime_logging
        configure_runtime_logging(database_path().parent)
    application = TrustedHostMiddleware(app, allowed_hosts=["127.0.0.1", "localhost"])
    port = int(os.getenv("BEINGS_LEADS_PORT", "8787"))
    if open_browser:
        config = uvicorn.Config(application, host=host, port=port, proxy_headers=False, workers=1)
        server = BrowserServer(config)
        # Claim the address before lifespan can open the database or start background work.
        with socket.socket() as listener:
            option = socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR
            listener.setsockopt(socket.SOL_SOCKET, option, 1)
            try:
                listener.bind((host, port))
            except OSError as exc:
                raise SystemExit(f"Could not start the dashboard at {host}:{port}: {exc}") from exc
            server.run(sockets=[listener])
        if not server.started:
            raise SystemExit(3)
    else:
        uvicorn.run(application, host=host, port=port, reload=False, proxy_headers=False)


if __name__ == "__main__":
    main()
