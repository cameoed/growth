from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .config import source_hydration_profile
from .creative_capacity import (
    CreativeCapacityHypothesis,
    CreativeCapacityMap,
    DeliveryOutcome,
    DiscoveryReview,
    EvidenceClaim,
    SalesAccount,
    SalesOutcome,
    StrategicQualification,
    evaluate_capacity_map,
    evaluate_strategic_qualification,
)
from .dataforseo_intelligence import EVIDENCE_DAYS as DATAFORSEO_EVIDENCE_DAYS
from .dataforseo_intelligence import REFRESH_DAYS as DATAFORSEO_REFRESH_DAYS
from .dataforseo_intelligence import SCANNER_ID as DATAFORSEO_SCANNER_ID
from .dataforseo_intelligence import TRAJECTORY_FEATURE_KEY as DATAFORSEO_TRAJECTORY_FEATURE_KEY
from .dataforseo_intelligence import (
    TRAJECTORY_SCORE_POLICY as DATAFORSEO_TRAJECTORY_SCORE_POLICY,
)
from .dataforseo_intelligence import seo_registered_domain
from .discovery import is_obvious_infrastructure_candidate
from .domain_admission import initialize_admission, register_hosts
from .domain_policy import is_disallowed, reconcile_disallowed_domains, require_allowed
from .error_recovery import UNRESOLVED
from .error_recovery import configure as configure_error_recovery
from .file_lock import open_directory_lock
from .models import (
    FRONTIER_SCANNER_IDS,
    AdIntelligence,
    AIAnalysis,
    AtomicObservation,
    CompanyAppearanceEvidence,
    DiscoveredHost,
    DiscoveryAssertionEvidence,
    EvidenceArtifact,
    FrontierIntelligence,
    RankedProspect,
    ScannerCoverageManifest,
    ScannerExecutionStatus,
    ScannerExecutionSummary,
    ScannerScheduleDecision,
    ScoreCard,
    SiteObservation,
    SourceSuggestion,
    legacy_agency_portfolio_domains,
)
from .naming import concise_company_name
from .platform_network_policy import (
    ApexFailureEvent,
    ApexFailureRecord,
    ApexFailureWindow,
    OpenedApexCircuit,
    is_recoverable_tenant_label,
    is_terminal_fetch_outcome,
)
from .rehydration import REHYDRATION_HOURS, REHYDRATION_INTERVAL
from .scan_diagnostics import child_step
from .source_identity import source_identity_key
from .source_registry import RegistrySource
from .storage_json import encode_text as encode_storage_json
from .storage_maintenance import enable_incremental_vacuum_for_empty_store
from .treg_people import MIN_GOOD_CONTACTS as TREG_PEOPLE_MIN_GOOD_CONTACTS
from .treg_people import REFRESH_DAYS as TREG_PEOPLE_REFRESH_DAYS
from .treg_people import SCANNER_ID as TREG_PEOPLE_SCANNER_ID
from .treg_people import SCANNER_VERSION as TREG_PEOPLE_SCANNER_VERSION
from .value_utils import naive_as_utc

OUTBOUND_ACTIVITY_TYPES = ("email_sent", "linkedin_message", "call", "outreach_recorded")
ACTIVITY_TYPES = {*OUTBOUND_ACTIVITY_TYPES, "reply", "meeting", "note"}
SNAPSHOT_SCORE_REINDEX = "snapshot-score-v9-conversion-intelligence-final"
TARGET_INTELLIGENCE_MIGRATION = "snapshot-target-intelligence-v10-conversion-intelligence-final"
COMPANY_SCAN_WORK_MIGRATION = "company-scan-work-v1"
COMPANY_SCAN_RUN_LEASE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_company_scan_work_running_run
    ON company_scan_work(claimed_run_id) WHERE status='running'
"""
COMPANY_SCAN_COMPLETION_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_company_scan_work_completed_candidate
    ON company_scan_work(completed_candidate_id, effort_key)
"""
COMPANY_SCAN_WORK_REPAIR_MIGRATION = "company-scan-work-v2-invalid-completion-repair"
COMPANY_SCAN_EFFORT = "website_hydration_v1"
COMPANY_SCAN_FRESH_HOURS = REHYDRATION_HOURS
COMPANY_SCAN_FAILURE_RETRY_HOURS = 24
COMPANY_SCAN_LEASE_MINUTES = 30
SOURCE_SUGGESTION_IDENTITY_MIGRATION = "source-suggestion-identity-v1"
SOURCE_SCOUT_QUERY_REUSE_DAYS = 90
SOURCE_SCOUT_QUERY_REUSE_RUNS = 100
SOURCE_SCOUT_QUERY_REUSE_PER_FAMILY = 256
FRONTIER_SCANNER_PERSISTENCE_MIGRATION = "frontier-scanner-persistence-v2-append-only"
MARKET_FEED_PERSISTENCE_MIGRATION = "market-feed-persistence-v1-append-only"
MARKET_EXACT_SUBJECT_ROLES = frozenset({"filer", "issuer", "registrant", "subject"})
SCANNER_WORK_FRESH_HOURS = REHYDRATION_HOURS
SCANNER_FAILURE_RETRY_HOURS = 24
SCANNER_WORK_LEASE_MINUTES = 30
SEO_READINESS_SCANNER_ID = "seo_visibility_engine_v1"
SEO_REFRESH_DAYS = DATAFORSEO_REFRESH_DAYS
SCANNER_REUSABLE_STATUSES = frozenset({"success", "success_empty", "not_applicable"})
SCANNER_FAILURE_STATUSES = frozenset(
    {
        "partial",
        "transient_error",
        "rate_limited",
        "robots_blocked",
        "identity_ambiguous",
        "failed",
    }
)
SCANNER_TERMINAL_STATUSES = SCANNER_REUSABLE_STATUSES | SCANNER_FAILURE_STATUSES
SCANNER_FAILURE_DEFAULT_RETRY_SECONDS = {
    "partial": 24 * 60 * 60,
    "transient_error": 24 * 60 * 60,
    "rate_limited": 24 * 60 * 60,
    "robots_blocked": 24 * 60 * 60,
    "identity_ambiguous": 24 * 60 * 60,
    "failed": 24 * 60 * 60,
}
SCAN_RELIABILITY_ATTEMPT_STATUSES = frozenset(
    {
        "success",
        "success_empty",
        "partial",
        "transient_error",
        "rate_limited",
        "robots_blocked",
        "identity_ambiguous",
        "failed",
    }
)
SCAN_RELIABILITY_FAILURE_STATUSES = frozenset(
    {
        "transient_error",
        "rate_limited",
        "robots_blocked",
        "identity_ambiguous",
        "failed",
    }
)
SCAN_RELIABILITY_MIN_ATTEMPTS = 3
SCAN_RELIABILITY_LOOKBACK_DAYS = 7
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 60_000
_INITIALIZED_DATABASES: dict[tuple[str, int, int], tuple[str, ...]] = {}
_DATABASE_INITIALIZATION_LOCK = Lock()
_ERROR_LOG_PRUNED_AT: dict[tuple[str, int, int], datetime] = {}
ERROR_LOG_RETENTION_DAYS = 7
TARGET_SCORE_THRESHOLD = 70
MAX_ACCOUNT_IMPORT_ROWS = 200
AD_DOMAIN_LINK_STATES = frozenset({"verified", "probable", "ambiguous", "rejected"})
AD_IDENTITY_STATES = frozenset({"unresolved", *AD_DOMAIN_LINK_STATES})
AD_DISCOVERY_TERMINAL_STATES = frozenset(
    {"complete", "partial", "failed", "cancelled", "interrupted"}
)
SOURCE_EXECUTION_TERMINAL_STATES = frozenset(
    {"complete", "partial", "failed", "cancelled", "interrupted"}
)
SOURCE_EXECUTION_ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
SOURCE_EXECUTION_RECOVERABLE_STATES = frozenset({"running", "cancelling"})
SOURCE_EXECUTION_COUNTER_FIELDS = (
    "sources_total",
    "sources_completed",
    "pages_fetched",
    "documents_fetched",
    "direct_candidates",
    "ai_candidates",
    "candidates_persisted",
    "candidates_hydrated",
    "direct_calls",
    "ai_calls",
    "ai_tool_calls",
)

TARGET_RELATIONSHIP_LANES = ("buyer", "referral_partner")
TARGET_SCOPES = ("direct", "adjacency", "unclassified", "out_of_scope")
TARGET_MANUAL_GATES = (
    {
        "key": "recurring_work_3_months",
        "label": "Recurring work for at least three months",
    },
    {
        "key": "three_immediate_asks",
        "label": "Exactly three credible first-month asks",
    },
    {"key": "economic_buyer", "label": "Named economic buyer"},
    {"key": "ai_permissible", "label": "AI-permissible work and rights"},
    {"key": "one_specialist_lane", "label": "One specialist delivery lane"},
    {
        "key": "annual_capacity_60k",
        "label": "Plausible $60,000 annual capacity",
    },
)
TARGET_PUBLIC_EVIDENCE_LIMITATIONS = (
    {
        "key": "ad_library_recency_and_spend",
        "label": "Current ad-library recency, active-ad count, and spend",
    },
    {
        "key": "team_size_and_ownership",
        "label": "Current creative team size, ownership, and available capacity",
    },
    {
        "key": "creative_testing_cadence",
        "label": "Current concept-testing cadence and asset fatigue",
    },
    {
        "key": "paid_media_optimization_context",
        "label": "Campaign structure, optimization event, and weekly optimized-event volume",
    },
    {
        "key": "budget_and_economic_buyer",
        "label": "Available budget and the accountable economic buyer",
    },
    {
        "key": "ai_permissibility",
        "label": "AI permissibility, rights, likeness, and brand-policy constraints",
    },
    {
        "key": "annual_capacity_60k",
        "label": "Plausible $60,000 annual capacity",
    },
    {
        "key": "three_immediate_asks",
        "label": "Exactly three credible first-month asks",
    },
)
TARGET_PROVIDER_ORIGINS = {
    "myvtex.com": ("vtex.com",),
    "vtexcommercestable.com.br": ("vtex.com",),
    "mybigcommerce.com": ("bigcommerce.com",),
    "myshopline.com": ("shopline.com",),
    "shoplineapp.com": ("shopline.com",),
    "3dcartstores.com": ("3dcart.com", "shift4shop.com"),
    "s4shops.com": ("shift4shop.com",),
    "myshoplaza.com": ("shoplazza.com",),
    "company.site": ("ecwid.com",),
    "mykajabi.com": ("kajabi.com",),
    "mktoweb.com": ("adobe.com", "marketo.com"),
    "hs-sites.com": ("hubspot.com",),
    "hs-sites-eu1.com": ("hubspot.com",),
    "hs-sites-na2.com": ("hubspot.com",),
    "hs-sites-na3.com": ("hubspot.com",),
    "hs-sites-ap1.com": ("hubspot.com",),
}
TARGET_PROVIDER_ORIGIN_SET = frozenset(
    provider for providers in TARGET_PROVIDER_ORIGINS.values() for provider in providers
)

TARGET_PROJECTION_SELECT = """
    ti.rubric_version AS target_rubric_version,
    ti.canonical_domain AS target_canonical_domain,
    ti.relationship_lane AS target_relationship_lane,
    ti.target_scope AS target_scope,
    ti.archetype AS target_archetype,
    ti.target_score AS target_score,
    ti.principal_signals_met AS target_principal_signals_met,
    ti.review_ready AS target_review_ready,
    ti.data_confidence AS target_data_confidence,
    ti.durable_fit_score AS target_durable_fit_score,
    ti.contact_now_score AS target_contact_now_score,
    ti.founder_priority_score AS target_founder_priority_score,
    ti.opportunity_risk AS target_opportunity_risk,
    ti.opportunity_uncertainty AS target_opportunity_uncertainty,
    ti.recommended_action AS target_recommended_action,
    ti.live AS target_live,
    ti.disqualified AS target_disqualified,
    ti.hypothesis_json AS target_hypothesis_json,
    ti.computed_at AS target_computed_at
"""


def _strategic_research_score_sql(alias: str) -> str:
    return (
        "CASE WHEN COALESCE(json_extract("
        f"{alias}.score_json, '$.creative_capacity.review_ready'), 0)=1 "
        "THEN COALESCE(json_extract("
        f"{alias}.score_json, '$.creative_capacity.total'), 0) ELSE 0 END"
    )


def _research_priority_sql(alias: str) -> str:
    return (
        f"MAX(COALESCE({alias}.final_score, 0), "
        f"COALESCE(json_extract({alias}.score_json, '$.contact_priority'), 0), "
        f"{_strategic_research_score_sql(alias)})"
    )


class AccountConflictError(ValueError):
    """A curated-account change would violate a lifecycle or ownership invariant."""


def normalize_account_website(value: str) -> tuple[str, str | None]:
    """Return a stable public origin and a dedupe host for an operator-entered website."""
    raw = value.strip()
    if not raw:
        return "", None
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Website must be a valid public HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Website URLs cannot contain credentials")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Website must contain a valid hostname") from exc
    dedupe_host = host.removeprefix("www.")
    require_allowed(dedupe_host)
    if (
        "." not in dedupe_host
        or len(dedupe_host) > 253
        or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in dedupe_host.split(".")
        )
    ):
        raise ValueError("Website must contain a valid public hostname")
    authority = dedupe_host
    if (
        port
        and not (parsed.scheme.lower() == "http" and port == 80)
        and not (parsed.scheme.lower() == "https" and port == 443)
    ):
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}", dedupe_host


def _target_is_suppressed(
    source_apex: str | None,
    hostname: str | None,
    canonical_domain: str | None,
) -> int:
    """Park infrastructure and provider-owned redirects without deleting inventory."""
    if is_disallowed(hostname or "") or is_disallowed(canonical_domain or ""):
        return 1
    source = (source_apex or "").strip().lower().rstrip(".")
    host = (hostname or "").strip().lower().rstrip(".")
    normalized_host = host.removeprefix("www.")
    canonical = (canonical_domain or "").strip().lower().rstrip(".").removeprefix("www.")
    if not source or not host:
        return 0
    try:
        candidate = DiscoveredHost(source_apex=source, hostname=host)
    except ValueError:
        return 0
    if is_obvious_infrastructure_candidate(candidate):
        return 1
    if (
        canonical in TARGET_PROVIDER_ORIGIN_SET
        and normalized_host != canonical
        and not normalized_host.endswith(f".{canonical}")
    ):
        return 1
    return int(
        any(
            canonical == provider or canonical.endswith(f".{provider}")
            for provider in TARGET_PROVIDER_ORIGINS.get(source, ())
        )
    )


TARGET_PROJECTION_SQL = """
INSERT INTO snapshot_target_intelligence(
    snapshot_id, candidate_id, rubric_version, canonical_domain,
    relationship_lane, target_scope, archetype, target_score,
    principal_signals_met, review_ready, data_confidence,
    durable_fit_score, contact_now_score, founder_priority_score,
    opportunity_risk, opportunity_uncertainty, recommended_action,
    live, disqualified, hypothesis_json, computed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(snapshot_id) DO UPDATE SET
    candidate_id=excluded.candidate_id,
    rubric_version=excluded.rubric_version,
    canonical_domain=excluded.canonical_domain,
    relationship_lane=excluded.relationship_lane,
    target_scope=excluded.target_scope,
    archetype=excluded.archetype,
    target_score=excluded.target_score,
    principal_signals_met=excluded.principal_signals_met,
    review_ready=excluded.review_ready,
    data_confidence=excluded.data_confidence,
    durable_fit_score=excluded.durable_fit_score,
    contact_now_score=excluded.contact_now_score,
    founder_priority_score=excluded.founder_priority_score,
    opportunity_risk=excluded.opportunity_risk,
    opportunity_uncertainty=excluded.opportunity_uncertainty,
    recommended_action=excluded.recommended_action,
    live=excluded.live,
    disqualified=excluded.disqualified,
    hypothesis_json=excluded.hypothesis_json,
    computed_at=excluded.computed_at
"""


DELETE_PROJECTED_EVENTS_SQL = "DELETE FROM snapshot_signal_events WHERE snapshot_id=?"


PROJECTED_EVENTS_SQL = """
INSERT INTO snapshot_signal_events(
    snapshot_id, candidate_id, company_key, event_key, event_type,
    signal_family, polarity, strength, current_strength, confidence,
    coverage, epistemic_status, independence_key, source_kind,
    source_url, observed_at, effective_at, expires_at, half_life_days,
    summary, payload_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


PROJECTED_RELATIONSHIPS_SQL = """
INSERT INTO company_relationships(
    subject_key, subject_type, relationship_type, object_key,
    object_type, object_name, source_url, confidence,
    first_observed_at, last_observed_at, payload_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(subject_key, relationship_type, object_key, source_url)
DO UPDATE SET
    object_name=excluded.object_name,
    confidence=excluded.confidence,
    status='active',
    last_observed_at=excluded.last_observed_at,
    payload_json=excluded.payload_json
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in tags[:20]:
        tag = re.sub(r"\s+", " ", "".join(char for char in raw if char.isprintable()))
        tag = tag.strip().lower()[:40]
        if tag and tag not in normalized:
            normalized.append(tag)
    return sorted(normalized)


@dataclass(frozen=True, slots=True)
class SnapshotPlanning:
    snapshot_id: int
    observed_at: str
    final_score: int
    live: bool
    has_error: bool
    budget_ready: bool
    target_strength: tuple[int, int, bool]


def observation_completes_company_work(observation: SiteObservation) -> bool:
    """Return whether an observation can safely satisfy reusable website hydration."""
    if observation.core_hydration_errors or observation.error or observation.fetch_outcome in {
        "retryable_error",
        "platform_blocked",
        "circuit_open",
    }:
        return False
    return not (
        observation.status_code is not None
        and observation.status_code >= 500
        and not is_terminal_fetch_outcome(observation)
    )


class LeadStore:
    def __init__(self, path: str | Path, *, initialize: bool = True,
                 check_same_thread: bool = True, read_only: bool = False,
                 maintenance: bool = False):
        if read_only and initialize:
            raise ValueError("Read-only stores cannot initialize schema")
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._maintenance_lock_fd = None
        if not maintenance:
            self._maintenance_lock_fd = open_directory_lock(self.path.parent, shared=True)
        try:
            if not maintenance:
                from .oracle_deployment import assert_local_store_allowed

                # Recheck after ownership: cutover can occur while a caller waits.
                assert_local_store_allowed(self.path)
            self.opened_read_only_identity = self._database_identity() if read_only else None
            self.connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro" if read_only else self.path,
                uri=read_only,
                timeout=SQLITE_BUSY_TIMEOUT_MILLISECONDS / 1_000,
                check_same_thread=check_same_thread,
            )
        except Exception as exc:
            if self._maintenance_lock_fd is not None:
                os.close(self._maintenance_lock_fd)
                self._maintenance_lock_fd = None
            if read_only and isinstance(exc, FileNotFoundError):
                raise sqlite3.OperationalError("unable to open database file") from exc
            raise
        from .storage_json import configure

        configure(self.connection)
        self.connection.create_function(
            "target_is_suppressed",
            3,
            _target_is_suppressed,
            deterministic=True,
        )
        self.connection.create_function(
            "seo_registered_domain", 1, seo_registered_domain, deterministic=True,
        )
        self.connection.execute(
            f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MILLISECONDS}"
        )
        self.connection.execute("PRAGMA foreign_keys=ON")
        if read_only:
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA cache_size=-4096")
            self.connection.execute("PRAGMA mmap_size=0")
        if not initialize:
            return
        try:
            with _DATABASE_INITIALIZATION_LOCK:
                identity = self._database_identity()
                migration_fingerprint = self._migration_fingerprint()
                if _INITIALIZED_DATABASES.get(identity) == migration_fingerprint:
                    reconcile_disallowed_domains(self.connection)
                    self._prune_error_logs_if_due(identity)
                    return
                journal_mode = str(
                    self.connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                enable_incremental_vacuum_for_empty_store(self.connection)
                if journal_mode != "wal":
                    self.connection.execute("PRAGMA journal_mode=WAL")
                self._initialize()
                initialize_admission(self.connection)
                reconcile_disallowed_domains(self.connection)
                self._prune_error_logs_if_due(identity)
                _INITIALIZED_DATABASES[self._database_identity()] = (
                    self._migration_fingerprint()
                )
        except Exception:
            self.close()
            raise

    def _database_identity(self) -> tuple[str, int, int]:
        stat = self.path.resolve().stat()
        return (str(self.path.resolve()), int(stat.st_dev), int(stat.st_ino))

    def _prune_error_logs_if_due(self, identity: tuple[str, int, int]) -> None:
        now = datetime.now(UTC)
        previous = _ERROR_LOG_PRUNED_AT.get(identity)
        if previous is not None and now - previous < timedelta(hours=1):
            return
        self.prune_error_logs(now=now)
        _ERROR_LOG_PRUNED_AT[identity] = now

    def prune_error_logs(self, *, now: datetime | None = None) -> dict[str, int]:
        """Expire diagnostic rows only; evidence, retry state and run totals survive."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=ERROR_LOG_RETENTION_DAYS)
        counts = {}
        with self.connection:
            for table, column, condition in (
                ("source_errors", "created_at", ""),
                ("smart_scan_errors", "timestamp", ""),
                ("smart_scan_events", "timestamp", " AND level='error'"),
            ):
                self.connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_log_age "
                    f"ON {table}(julianday({column}))"
                )
                cursor = self.connection.execute(
                    f"DELETE FROM {table} WHERE julianday({column}) < julianday(?)"
                    f"{condition}", (cutoff.isoformat(),),
                )
                counts[table] = cursor.rowcount
        return counts

    def _migration_fingerprint(self) -> tuple[str, ...]:
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return ()
        return tuple(
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            ).fetchall()
        )

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            if self._maintenance_lock_fd is not None:
                os.close(self._maintenance_lock_fd)
                self._maintenance_lock_fd = None

    def __enter__(self) -> LeadStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def active_platform_apex_cooldowns(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        timestamp = self._utc_datetime(now or datetime.now(UTC)).isoformat()
        rows = self.connection.execute(
            """
            SELECT apex, outcome_signature, threshold, opened_at,
                   cooldown_until, sample_detail, updated_at
            FROM platform_apex_cooldowns
            WHERE cooldown_until>?
            ORDER BY apex
            """,
            (timestamp,),
        ).fetchall()
        return {str(row["apex"]): dict(row) for row in rows}

    def platform_apex_cooldown(
        self,
        apex: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        normalized_apex = self._normalize_identity_host(apex)
        if not normalized_apex:
            return None
        timestamp = self._utc_datetime(now or datetime.now(UTC)).isoformat()
        row = self.connection.execute(
            """
            SELECT apex, outcome_signature, threshold, opened_at,
                   cooldown_until, sample_detail, updated_at
            FROM platform_apex_cooldowns
            WHERE apex=? AND cooldown_until>?
            """,
            (normalized_apex, timestamp),
        ).fetchone()
        return dict(row) if row is not None else None

    def open_platform_apex_cooldown(
        self,
        *,
        apex: str,
        outcome_signature: str,
        threshold: int,
        opened_at: str,
        cooldown_until: str,
        sample_detail: str,
    ) -> None:
        normalized_apex = self._normalize_identity_host(apex)
        if not normalized_apex:
            raise ValueError("Platform apex cannot be blank")
        self.connection.execute(
            """
            INSERT INTO platform_apex_cooldowns(
                apex, outcome_signature, threshold, opened_at,
                cooldown_until, sample_detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apex) DO UPDATE SET
                outcome_signature=excluded.outcome_signature,
                threshold=excluded.threshold,
                opened_at=excluded.opened_at,
                cooldown_until=excluded.cooldown_until,
                sample_detail=excluded.sample_detail,
                updated_at=excluded.updated_at
            """,
            (
                normalized_apex,
                str(outcome_signature)[:80],
                max(1, int(threshold)),
                str(opened_at),
                str(cooldown_until),
                " ".join(str(sample_detail).split())[:500],
                _now(),
            ),
        )
        self.connection.commit()

    def active_platform_apex_failure_windows(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT apex, failure_count, signature_counts_json,
                   sample_detail, updated_at
            FROM platform_apex_failure_windows
            ORDER BY apex
            """
        ).fetchall()
        windows: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                raw_counts = json.loads(row["signature_counts_json"] or "{}")
                failure_count = int(row["failure_count"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw_counts, dict) or failure_count < 1:
                continue
            signature_counts: dict[str, int] = {}
            for raw_signature, raw_count in raw_counts.items():
                signature = str(raw_signature).strip()[:80]
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    continue
                if signature and count > 0:
                    signature_counts[signature] = count
            represented = sum(signature_counts.values())
            if represented < failure_count:
                signature_counts["platform_error"] = (
                    signature_counts.get("platform_error", 0)
                    + failure_count
                    - represented
                )
            elif represented > failure_count:
                failure_count = represented
            windows[str(row["apex"])] = {
                "apex": str(row["apex"]),
                "failure_count": failure_count,
                "signature_counts": signature_counts,
                "sample_detail": str(row["sample_detail"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
        return windows

    def save_platform_apex_failure_window(
        self,
        *,
        apex: str,
        failure_count: int,
        signature_counts: dict[str, int],
        sample_detail: str,
        updated_at: str,
    ) -> None:
        normalized_apex = self._normalize_identity_host(apex)
        if not normalized_apex:
            raise ValueError("Platform apex cannot be blank")
        normalized_count = max(1, int(failure_count))
        normalized_signatures: dict[str, int] = {}
        for raw_signature, raw_count in signature_counts.items():
            signature = str(raw_signature).strip()[:80]
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if signature and count > 0:
                normalized_signatures[signature] = count
        if not normalized_signatures:
            raise ValueError("Platform failure window requires a signature")
        self.connection.execute(
            """
            INSERT INTO platform_apex_failure_windows(
                apex, failure_count, signature_counts_json,
                sample_detail, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(apex) DO UPDATE SET
                failure_count=excluded.failure_count,
                signature_counts_json=excluded.signature_counts_json,
                sample_detail=excluded.sample_detail,
                updated_at=excluded.updated_at
            """,
            (
                normalized_apex,
                normalized_count,
                json.dumps(normalized_signatures, sort_keys=True),
                " ".join(str(sample_detail).split())[:500],
                str(updated_at),
            ),
        )
        self.connection.commit()

    def clear_platform_apex_failure_window(self, apex: str) -> bool:
        normalized_apex = self._normalize_identity_host(apex)
        if not normalized_apex:
            return False
        cursor = self.connection.execute(
            "DELETE FROM platform_apex_failure_windows WHERE apex=?",
            (normalized_apex,),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def record_platform_apex_failure(
        self,
        event: ApexFailureEvent,
    ) -> ApexFailureRecord:
        """Atomically increment an apex window and open its cooldown at threshold."""
        normalized_apex = self._normalize_identity_host(event.apex)
        signature = str(event.signature).strip()[:80]
        if not normalized_apex or not signature:
            raise ValueError("Platform failure event requires an apex and signature")
        attempt_count = max(1, int(event.attempt_count))
        threshold = max(1, int(event.threshold))
        cooldown_hours = max(1, int(event.cooldown_hours))
        try:
            observed = self._utc_datetime(datetime.fromisoformat(event.observed_at))
        except (TypeError, ValueError) as exc:
            raise ValueError("Platform failure event has an invalid timestamp") from exc
        observed_at = observed.isoformat()
        sample_detail = " ".join(str(event.sample_detail).split())[:500]

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT failure_count, signature_counts_json
                FROM platform_apex_failure_windows
                WHERE apex=?
                """,
                (normalized_apex,),
            ).fetchone()
            failure_count = 0
            signature_counts: dict[str, int] = {}
            if row is not None:
                try:
                    failure_count = max(0, int(row["failure_count"]))
                    raw_counts = json.loads(row["signature_counts_json"] or "{}")
                    if isinstance(raw_counts, dict):
                        for raw_signature, raw_count in raw_counts.items():
                            normalized_signature = str(raw_signature).strip()[:80]
                            try:
                                count = int(raw_count)
                            except (TypeError, ValueError):
                                continue
                            if normalized_signature and count > 0:
                                signature_counts[normalized_signature] = count
                except (TypeError, ValueError, json.JSONDecodeError):
                    signature_counts = {}
                represented = sum(signature_counts.values())
                if represented < failure_count:
                    signature_counts["platform_error"] = failure_count - represented
                elif represented > failure_count:
                    failure_count = represented
            failure_count += attempt_count
            signature_counts[signature] = signature_counts.get(signature, 0) + attempt_count
            self.connection.execute(
                """
                INSERT INTO platform_apex_failure_windows(
                    apex, failure_count, signature_counts_json,
                    sample_detail, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(apex) DO UPDATE SET
                    failure_count=excluded.failure_count,
                    signature_counts_json=excluded.signature_counts_json,
                    sample_detail=excluded.sample_detail,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_apex,
                    failure_count,
                    json.dumps(signature_counts, sort_keys=True),
                    sample_detail,
                    observed_at,
                ),
            )
            window = ApexFailureWindow(
                apex=normalized_apex,
                failure_count=failure_count,
                signature_counts=signature_counts,
                sample_detail=sample_detail,
                updated_at=observed_at,
            )

            active_row = self.connection.execute(
                """
                SELECT outcome_signature, threshold, opened_at,
                       cooldown_until, sample_detail
                FROM platform_apex_cooldowns
                WHERE apex=? AND cooldown_until>?
                """,
                (normalized_apex, observed_at),
            ).fetchone()
            circuit: OpenedApexCircuit | None = None
            opened = False
            if active_row is not None:
                circuit = OpenedApexCircuit(
                    apex=normalized_apex,
                    outcome_signature=str(active_row["outcome_signature"]),
                    threshold=int(active_row["threshold"]),
                    opened_at=str(active_row["opened_at"]),
                    cooldown_until=str(active_row["cooldown_until"]),
                    sample_detail=str(active_row["sample_detail"] or ""),
                )
            elif failure_count >= threshold:
                outcome_signature = (
                    signature if len(signature_counts) == 1 else "mixed_platform_failures"
                )
                breakdown = ", ".join(
                    f"{name}={count}"
                    for name, count in sorted(
                        signature_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                )
                circuit_detail = sample_detail
                if len(signature_counts) > 1:
                    circuit_detail = (
                        f"{circuit_detail}; consecutive signatures: {breakdown}"
                    )[:500]
                cooldown_until = (observed + timedelta(hours=cooldown_hours)).isoformat()
                circuit = OpenedApexCircuit(
                    apex=normalized_apex,
                    outcome_signature=outcome_signature,
                    threshold=threshold,
                    opened_at=observed_at,
                    cooldown_until=cooldown_until,
                    sample_detail=circuit_detail,
                )
                self.connection.execute(
                    """
                    INSERT INTO platform_apex_cooldowns(
                        apex, outcome_signature, threshold, opened_at,
                        cooldown_until, sample_detail, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(apex) DO UPDATE SET
                        outcome_signature=excluded.outcome_signature,
                        threshold=excluded.threshold,
                        opened_at=excluded.opened_at,
                        cooldown_until=excluded.cooldown_until,
                        sample_detail=excluded.sample_detail,
                        updated_at=excluded.updated_at
                    """,
                    (
                        circuit.apex,
                        circuit.outcome_signature,
                        circuit.threshold,
                        circuit.opened_at,
                        circuit.cooldown_until,
                        circuit.sample_detail,
                        observed_at,
                    ),
                )
                opened = True
            self.connection.commit()
            return ApexFailureRecord(window=window, circuit=circuit, opened=opened)
        except Exception:
            self.connection.rollback()
            raise

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS company_email_lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_key TEXT NOT NULL,
                person_key TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                work_email TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL DEFAULT '',
                cost_microusd INTEGER NOT NULL DEFAULT 0,
                email_verified INTEGER NOT NULL DEFAULT 0,
                error_json TEXT NOT NULL DEFAULT '{}',
                maximum_microusd INTEGER NOT NULL DEFAULT 25000
            );
            CREATE INDEX IF NOT EXISTS idx_company_email_lookups
                ON company_email_lookups(company_key, person_key, id DESC);
            CREATE TABLE IF NOT EXISTS company_email_dispatch (
                lookup_id INTEGER PRIMARY KEY
                    REFERENCES company_email_lookups(id) ON DELETE CASCADE,
                run_token TEXT NOT NULL, dispatched_at TEXT, released_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_company_email_dispatch_run
                ON company_email_dispatch(run_token,dispatched_at);
            CREATE TABLE IF NOT EXISTS treg_call_recovery (
                call_id TEXT PRIMARY KEY,
                company_key TEXT NOT NULL,
                workflow TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                config_json TEXT NOT NULL,
                summary_json TEXT
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_apex TEXT NOT NULL,
                hostname TEXT NOT NULL UNIQUE,
                first_seen TEXT,
                label_hint TEXT NOT NULL DEFAULT '',
                discovery_rank REAL NOT NULL DEFAULT 0,
                discovery_evidence_json TEXT NOT NULL DEFAULT '[]',
                provider_record_ids_json TEXT NOT NULL DEFAULT '[]',
                discovery_assertions_json TEXT NOT NULL DEFAULT '[]',
                discovered_at TEXT NOT NULL,
                last_discovered_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source_apex);
            CREATE TABLE IF NOT EXISTS candidate_source_edges (
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                source_apex TEXT NOT NULL,
                discovery_rank REAL NOT NULL DEFAULT 0,
                label_hint TEXT NOT NULL DEFAULT '',
                evidence_urls_json TEXT NOT NULL DEFAULT '[]',
                provider_record_ids_json TEXT NOT NULL DEFAULT '[]',
                discovery_assertions_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, source_apex)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_source_edges_source
                ON candidate_source_edges(source_apex, candidate_id);
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                run_id INTEGER NOT NULL REFERENCES runs(id),
                observed_at TEXT NOT NULL,
                observation_json TEXT NOT NULL,
                score_json TEXT NOT NULL,
                ai_json TEXT,
                final_score INTEGER NOT NULL,
                material_hash TEXT NOT NULL,
                UNIQUE(candidate_id, run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_run_score
                ON snapshots(run_id, final_score DESC);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ai_recovery
                ON snapshots(candidate_id, id DESC)
                WHERE CASE WHEN json_valid(observation_json)
                    AND (ai_json IS NULL OR json_valid(ai_json)) THEN
                    COALESCE(json_extract(observation_json, '$.canonical_recovery_required'), 0)=1
                    AND COALESCE(json_extract(ai_json, '$.company_url'), '')=''
                    ELSE 0 END;
            CREATE TABLE IF NOT EXISTS snapshot_target_intelligence (
                snapshot_id INTEGER PRIMARY KEY
                    REFERENCES snapshots(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                rubric_version TEXT NOT NULL,
                canonical_domain TEXT NOT NULL,
                relationship_lane TEXT NOT NULL,
                target_scope TEXT NOT NULL,
                archetype TEXT NOT NULL,
                target_score INTEGER NOT NULL,
                principal_signals_met INTEGER NOT NULL,
                review_ready INTEGER NOT NULL,
                data_confidence INTEGER NOT NULL,
                durable_fit_score INTEGER NOT NULL DEFAULT 0,
                contact_now_score INTEGER NOT NULL DEFAULT 0,
                founder_priority_score INTEGER NOT NULL DEFAULT 0,
                opportunity_risk INTEGER NOT NULL DEFAULT 0,
                opportunity_uncertainty INTEGER NOT NULL DEFAULT 100,
                recommended_action TEXT NOT NULL DEFAULT 'research_next',
                live INTEGER NOT NULL,
                disqualified INTEGER NOT NULL,
                hypothesis_json TEXT NOT NULL,
                computed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshot_target_candidate
                ON snapshot_target_intelligence(candidate_id, snapshot_id DESC);
            CREATE INDEX IF NOT EXISTS idx_snapshot_target_ranking
                ON snapshot_target_intelligence(
                    relationship_lane, target_scope, live, disqualified,
                    review_ready DESC, target_score DESC,
                    principal_signals_met DESC, data_confidence DESC
                );
            CREATE INDEX IF NOT EXISTS idx_snapshot_target_canonical
                ON snapshot_target_intelligence(canonical_domain, snapshot_id DESC);
            CREATE TABLE IF NOT EXISTS snapshot_signal_events (
                snapshot_id INTEGER NOT NULL
                    REFERENCES snapshots(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                company_key TEXT NOT NULL,
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                signal_family TEXT NOT NULL,
                polarity TEXT NOT NULL,
                strength INTEGER NOT NULL,
                current_strength REAL NOT NULL,
                confidence TEXT NOT NULL,
                coverage TEXT NOT NULL,
                epistemic_status TEXT NOT NULL,
                independence_key TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_url TEXT,
                observed_at TEXT,
                effective_at TEXT,
                expires_at TEXT,
                half_life_days REAL NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(snapshot_id, event_key)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshot_signal_company_time
                ON snapshot_signal_events(company_key, effective_at DESC, event_type);
            CREATE INDEX IF NOT EXISTS idx_snapshot_signal_family_current
                ON snapshot_signal_events(
                    signal_family, polarity, current_strength DESC, effective_at DESC
                );
            CREATE TABLE IF NOT EXISTS company_relationships (
                subject_key TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                object_key TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_name TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL,
                confidence TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(
                    subject_key, relationship_type, object_key, source_url
                )
            );
            CREATE INDEX IF NOT EXISTS idx_company_relationship_subject
                ON company_relationships(subject_key, relationship_type, last_observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_company_relationship_object
                ON company_relationships(object_key, relationship_type, last_observed_at DESC);
            CREATE TABLE IF NOT EXISTS company_scan_work (
                company_key TEXT NOT NULL,
                effort_key TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('running', 'complete', 'failed')),
                claimed_candidate_id INTEGER
                    REFERENCES candidates(id) ON DELETE SET NULL,
                claimed_run_id INTEGER
                    REFERENCES runs(id) ON DELETE SET NULL,
                claimed_at TEXT,
                lease_expires_at TEXT,
                completed_candidate_id INTEGER
                    REFERENCES candidates(id) ON DELETE SET NULL,
                completed_run_id INTEGER
                    REFERENCES runs(id) ON DELETE SET NULL,
                completed_snapshot_id INTEGER
                    REFERENCES snapshots(id) ON DELETE SET NULL,
                completed_at TEXT,
                next_retry_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(company_key, effort_key)
            );
            CREATE INDEX IF NOT EXISTS idx_company_scan_work_active_lease
                ON company_scan_work(effort_key, status, lease_expires_at);
            CREATE INDEX IF NOT EXISTS idx_company_scan_work_completion
                ON company_scan_work(effort_key, status, completed_at DESC);
            CREATE TABLE IF NOT EXISTS platform_apex_cooldowns (
                apex TEXT PRIMARY KEY,
                outcome_signature TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                cooldown_until TEXT NOT NULL,
                sample_detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_apex_cooldowns_expiry
                ON platform_apex_cooldowns(cooldown_until);
            CREATE TABLE IF NOT EXISTS platform_apex_failure_windows (
                apex TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL CHECK(failure_count >= 1),
                signature_counts_json TEXT NOT NULL DEFAULT '{}',
                sample_detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                rows_changed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS run_progress (
                run_id INTEGER PRIMARY KEY REFERENCES runs(id),
                phase TEXT NOT NULL DEFAULT 'discovering',
                discovered INTEGER NOT NULL DEFAULT 0,
                inspected INTEGER NOT NULL DEFAULT 0,
                total_candidates INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                run_id INTEGER NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
            CREATE TABLE IF NOT EXISTS source_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES runs(id),
                source_apex TEXT NOT NULL,
                error TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS smart_scan_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                control_status TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_smart_scan_runs_updated
                ON smart_scan_runs(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_smart_scan_runs_recovery
                ON smart_scan_runs(control_status, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS smart_scan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                smart_scan_id TEXT NOT NULL
                    REFERENCES smart_scan_runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                phase TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                UNIQUE(smart_scan_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_smart_scan_events_recent
                ON smart_scan_events(smart_scan_id, sequence DESC);
            CREATE TABLE IF NOT EXISTS smart_scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                smart_scan_id TEXT NOT NULL
                    REFERENCES smart_scan_runs(id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL,
                phase TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_smart_scan_errors_recent
                ON smart_scan_errors(smart_scan_id, id DESC);
            CREATE TABLE IF NOT EXISTS managed_sources (
                apex TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'Other',
                tier TEXT NOT NULL DEFAULT 'Research',
                indexed_names INTEGER,
                expected_fit TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                registry_managed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_managed_sources_enabled
                ON managed_sources(enabled, tier, apex);
            CREATE TABLE IF NOT EXISTS prospect_workflow (
                candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id),
                stage TEXT NOT NULL DEFAULT 'research',
                starred INTEGER NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0,
                scan_enabled INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_prospect_workflow_stage
                ON prospect_workflow(stage, starred);
            CREATE TABLE IF NOT EXISTS prospect_tags (
                candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_prospect_tags_tag
                ON prospect_tags(tag, candidate_id);
            CREATE TABLE IF NOT EXISTS prospect_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                contact_email TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_prospect_activities_candidate_time
                ON prospect_activities(candidate_id, occurred_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS intelligence_exposures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                activity_id INTEGER NOT NULL UNIQUE
                    REFERENCES prospect_activities(id) ON DELETE CASCADE,
                snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
                channel TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                rubric_version TEXT NOT NULL DEFAULT '',
                target_score INTEGER NOT NULL DEFAULT 0,
                founder_priority_score INTEGER NOT NULL DEFAULT 0,
                recommended_action TEXT NOT NULL DEFAULT '',
                message_angle TEXT NOT NULL DEFAULT '',
                trigger_event_keys_json TEXT NOT NULL DEFAULT '[]',
                source_paths_json TEXT NOT NULL DEFAULT '[]',
                projection_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_intelligence_exposures_candidate_time
                ON intelligence_exposures(candidate_id, occurred_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS ai_cache (
                content_key TEXT PRIMARY KEY,
                analysis_json TEXT NOT NULL,
                model TEXT NOT NULL,
                web_search INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_cache_last_used
                ON ai_cache(last_used_at DESC);
            CREATE TABLE IF NOT EXISTS ai_review_receipts (
                content_key TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_review_receipts_scope
                ON ai_review_receipts(hostname,model,prompt_version,completed_at);
            CREATE TABLE IF NOT EXISTS free_signal_cache (
                host TEXT NOT NULL,
                signal_kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                cache_status TEXT NOT NULL DEFAULT 'success',
                error_count INTEGER NOT NULL DEFAULT 0,
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY(host, signal_kind)
            );
            CREATE INDEX IF NOT EXISTS idx_free_signal_cache_expiry
                ON free_signal_cache(expires_at, signal_kind);
            CREATE TABLE IF NOT EXISTS source_scout_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                model TEXT NOT NULL,
                max_tool_calls INTEGER NOT NULL,
                summary_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_source_scout_runs_status
                ON source_scout_runs(status, id DESC);
            CREATE TABLE IF NOT EXISTS source_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                source_identity_key TEXT NOT NULL DEFAULT '',
                duplicate_of_suggestion_id INTEGER
                    REFERENCES source_suggestions(id) ON DELETE RESTRICT,
                source_kind TEXT NOT NULL,
                apex TEXT,
                url TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                relationship_type TEXT NOT NULL DEFAULT 'buyer',
                source_family TEXT NOT NULL DEFAULT 'other',
                buyer_archetypes_json TEXT NOT NULL DEFAULT '[]',
                discovery_mechanism TEXT NOT NULL,
                expected_6k_fit TEXT NOT NULL,
                confidence TEXT NOT NULL,
                rationale_json TEXT NOT NULL,
                evidence_urls_json TEXT NOT NULL,
                intelligence_score INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                scout_run_id INTEGER REFERENCES source_scout_runs(id),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_source_suggestions_status_score
                ON source_suggestions(status, intelligence_score DESC, id DESC);
            CREATE TABLE IF NOT EXISTS source_execution_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                scan_job_id TEXT,
                scan_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                method TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                phase TEXT NOT NULL DEFAULT 'starting',
                suggestion_ids_json TEXT NOT NULL DEFAULT '[]',
                request_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT,
                sources_total INTEGER NOT NULL DEFAULT 0 CHECK(sources_total >= 0),
                sources_completed INTEGER NOT NULL DEFAULT 0 CHECK(sources_completed >= 0),
                pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK(pages_fetched >= 0),
                documents_fetched INTEGER NOT NULL DEFAULT 0 CHECK(documents_fetched >= 0),
                direct_candidates INTEGER NOT NULL DEFAULT 0 CHECK(direct_candidates >= 0),
                ai_candidates INTEGER NOT NULL DEFAULT 0 CHECK(ai_candidates >= 0),
                candidates_persisted INTEGER NOT NULL DEFAULT 0
                    CHECK(candidates_persisted >= 0),
                candidates_hydrated INTEGER NOT NULL DEFAULT 0
                    CHECK(candidates_hydrated >= 0),
                direct_calls INTEGER NOT NULL DEFAULT 0 CHECK(direct_calls >= 0),
                ai_calls INTEGER NOT NULL DEFAULT 0 CHECK(ai_calls >= 0),
                ai_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(ai_tool_calls >= 0),
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_source_execution_runs_status
                ON source_execution_runs(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_source_execution_runs_started
                ON source_execution_runs(started_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS discovery_source_candidates (
                execution_run_id INTEGER NOT NULL
                    REFERENCES source_execution_runs(id) ON DELETE CASCADE,
                suggestion_id INTEGER NOT NULL
                    REFERENCES source_suggestions(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                source_apex TEXT NOT NULL,
                label_hint TEXT NOT NULL DEFAULT '',
                discovery_rank REAL NOT NULL DEFAULT 0,
                evidence_urls_json TEXT NOT NULL DEFAULT '[]',
                provider_record_ids_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(execution_run_id, suggestion_id, candidate_id)
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_source_candidates_candidate
                ON discovery_source_candidates(candidate_id, execution_run_id DESC);
            CREATE INDEX IF NOT EXISTS idx_discovery_source_candidates_suggestion
                ON discovery_source_candidates(suggestion_id, execution_run_id DESC);
            CREATE TABLE IF NOT EXISTS ad_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                provider TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'queued',
                request_json TEXT NOT NULL,
                estimate_json TEXT NOT NULL,
                rate_card_version TEXT NOT NULL DEFAULT '',
                approved_cap_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(approved_cap_microusd >= 0),
                estimated_cost_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(estimated_cost_microusd >= 0),
                actual_cost_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(actual_cost_microusd >= 0),
                provider_run_ids_json TEXT NOT NULL DEFAULT '[]',
                provider_dataset_ids_json TEXT NOT NULL DEFAULT '[]',
                queries_total INTEGER NOT NULL DEFAULT 0,
                queries_completed INTEGER NOT NULL DEFAULT 0,
                advertisers_found INTEGER NOT NULL DEFAULT 0,
                links_verified INTEGER NOT NULL DEFAULT 0,
                candidates_promoted INTEGER NOT NULL DEFAULT 0,
                creatives_found INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT NOT NULL DEFAULT '',
                summary_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ad_discovery_runs_status
                ON ad_discovery_runs(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ad_discovery_runs_started
                ON ad_discovery_runs(started_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS ad_discovery_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL
                    REFERENCES ad_discovery_runs(id) ON DELETE CASCADE,
                query_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                surface TEXT NOT NULL,
                phase TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                input_url TEXT NOT NULL DEFAULT '',
                result_cap INTEGER NOT NULL DEFAULT 0 CHECK(result_cap >= 0),
                result_count INTEGER NOT NULL DEFAULT 0 CHECK(result_count >= 0),
                unique_advertisers INTEGER NOT NULL DEFAULT 0
                    CHECK(unique_advertisers >= 0),
                estimated_cost_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(estimated_cost_microusd >= 0),
                actual_cost_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(actual_cost_microusd >= 0),
                status TEXT NOT NULL DEFAULT 'queued',
                provider_run_id TEXT NOT NULL DEFAULT '',
                provider_dataset_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, query_key)
            );
            CREATE INDEX IF NOT EXISTS idx_ad_discovery_queries_run
                ON ad_discovery_queries(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_ad_discovery_queries_status
                ON ad_discovery_queries(status, run_id, id);
            CREATE TABLE IF NOT EXISTS advertiser_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                handle TEXT NOT NULL DEFAULT '',
                profile_url TEXT NOT NULL DEFAULT '',
                linked_instagram_handle TEXT NOT NULL DEFAULT '',
                coverage_type TEXT NOT NULL DEFAULT '',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(platform, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_advertiser_entities_handle
                ON advertiser_entities(platform, handle);
            CREATE INDEX IF NOT EXISTS idx_advertiser_entities_name
                ON advertiser_entities(platform, display_name);
            CREATE TABLE IF NOT EXISTS advertiser_domain_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                advertiser_id INTEGER NOT NULL
                    REFERENCES advertiser_entities(id) ON DELETE CASCADE,
                candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
                domain TEXT NOT NULL,
                landing_host TEXT NOT NULL DEFAULT '',
                landing_url TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'probable',
                confidence INTEGER NOT NULL DEFAULT 0
                    CHECK(confidence BETWEEN 0 AND 100),
                landing_domain_match INTEGER NOT NULL DEFAULT 0,
                social_handle_match INTEGER NOT NULL DEFAULT 0,
                name_match INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                verified_at TEXT,
                reviewed_at TEXT,
                reviewed_by TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',
                UNIQUE(advertiser_id, domain)
            );
            CREATE INDEX IF NOT EXISTS idx_advertiser_domain_links_state
                ON advertiser_domain_links(state, confidence DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_advertiser_domain_links_candidate
                ON advertiser_domain_links(candidate_id, state, confidence DESC);
            CREATE INDEX IF NOT EXISTS idx_advertiser_domain_links_domain
                ON advertiser_domain_links(domain, state, confidence DESC);
            CREATE TABLE IF NOT EXISTS ad_creatives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                advertiser_id INTEGER NOT NULL
                    REFERENCES advertiser_entities(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                creative_group_id TEXT NOT NULL DEFAULT '',
                creative_fingerprint TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                ended_at TEXT,
                publisher_platforms_json TEXT NOT NULL DEFAULT '[]',
                body TEXT NOT NULL DEFAULT '',
                headline TEXT NOT NULL DEFAULT '',
                cta TEXT NOT NULL DEFAULT '',
                landing_url TEXT NOT NULL DEFAULT '',
                landing_host TEXT NOT NULL DEFAULT '',
                landing_domain TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                coverage_type TEXT NOT NULL DEFAULT '',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(platform, advertiser_id, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ad_creatives_advertiser
                ON ad_creatives(advertiser_id, is_active, last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ad_creatives_group
                ON ad_creatives(platform, advertiser_id, creative_group_id);
            CREATE INDEX IF NOT EXISTS idx_ad_creatives_landing_domain
                ON ad_creatives(landing_domain, last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS ad_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL
                    REFERENCES ad_discovery_runs(id) ON DELETE CASCADE,
                query_id INTEGER NOT NULL
                    REFERENCES ad_discovery_queries(id) ON DELETE CASCADE,
                advertiser_id INTEGER
                    REFERENCES advertiser_entities(id) ON DELETE SET NULL,
                creative_id INTEGER
                    REFERENCES ad_creatives(id) ON DELETE SET NULL,
                item_key TEXT NOT NULL,
                provider_item_id TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                active INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(run_id, query_id, item_key)
            );
            CREATE INDEX IF NOT EXISTS idx_ad_run_items_run
                ON ad_run_items(run_id, query_id, id);
            CREATE INDEX IF NOT EXISTS idx_ad_run_items_advertiser
                ON ad_run_items(advertiser_id, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ad_run_items_creative
                ON ad_run_items(creative_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS candidate_ad_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL
                    REFERENCES candidates(id) ON DELETE CASCADE,
                run_id INTEGER NOT NULL
                    REFERENCES ad_discovery_runs(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                surface TEXT NOT NULL,
                coverage_type TEXT NOT NULL DEFAULT '',
                identity_state TEXT NOT NULL DEFAULT 'unresolved',
                identity_confidence INTEGER NOT NULL DEFAULT 0
                    CHECK(identity_confidence BETWEEN 0 AND 100),
                active_ad_count INTEGER NOT NULL DEFAULT 0
                    CHECK(active_ad_count >= 0),
                total_ad_count INTEGER NOT NULL DEFAULT 0
                    CHECK(total_ad_count >= 0),
                active_creative_groups INTEGER NOT NULL DEFAULT 0
                    CHECK(active_creative_groups >= 0),
                total_creative_groups INTEGER NOT NULL DEFAULT 0
                    CHECK(total_creative_groups >= 0),
                new_creatives_7d INTEGER NOT NULL DEFAULT 0
                    CHECK(new_creatives_7d >= 0),
                new_creatives_30d INTEGER NOT NULL DEFAULT 0
                    CHECK(new_creatives_30d >= 0),
                new_creatives_90d INTEGER NOT NULL DEFAULT 0
                    CHECK(new_creatives_90d >= 0),
                video_creatives INTEGER NOT NULL DEFAULT 0
                    CHECK(video_creatives >= 0),
                image_creatives INTEGER NOT NULL DEFAULT 0
                    CHECK(image_creatives >= 0),
                first_started_at TEXT,
                latest_started_at TEXT,
                publisher_platforms_json TEXT NOT NULL DEFAULT '[]',
                creative_group_ids_json TEXT NOT NULL DEFAULT '[]',
                active_creative_group_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_urls_json TEXT NOT NULL DEFAULT '[]',
                coverage_notes_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT NOT NULL,
                UNIQUE(candidate_id, run_id, platform, surface)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_ad_observations_current
                ON candidate_ad_observations(
                    candidate_id, platform, surface, observed_at DESC, id DESC
                );
            CREATE INDEX IF NOT EXISTS idx_candidate_ad_observations_run
                ON candidate_ad_observations(run_id, candidate_id, id);
            CREATE TABLE IF NOT EXISTS sales_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER UNIQUE REFERENCES candidates(id),
                company TEXT NOT NULL,
                website TEXT NOT NULL DEFAULT '',
                website_host TEXT,
                cohort TEXT NOT NULL DEFAULT 'prototype-v1',
                relationship_type TEXT NOT NULL DEFAULT 'buyer',
                segment TEXT NOT NULL DEFAULT 'other',
                lead_source TEXT NOT NULL DEFAULT 'targeted_outbound',
                referrer TEXT NOT NULL DEFAULT '',
                intro_status TEXT NOT NULL DEFAULT 'none',
                notes TEXT NOT NULL DEFAULT '',
                qualification_json TEXT NOT NULL DEFAULT '{}',
                capacity_map_json TEXT NOT NULL DEFAULT '{}',
                discovery_json TEXT NOT NULL DEFAULT '{}',
                outcome_json TEXT NOT NULL DEFAULT '{}',
                delivery_json TEXT NOT NULL DEFAULT '{}',
                qualification_state TEXT NOT NULL DEFAULT 'needs_research',
                qualification_score INTEGER NOT NULL DEFAULT 0,
                lifecycle TEXT NOT NULL DEFAULT 'research',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_accounts_website_host
                ON sales_accounts(website_host) WHERE website_host IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_sales_accounts_cohort_state_score
                ON sales_accounts(cohort, qualification_state, qualification_score DESC);
            CREATE INDEX IF NOT EXISTS idx_sales_accounts_relationship_segment
                ON sales_accounts(relationship_type, segment, lifecycle);
            CREATE INDEX IF NOT EXISTS idx_sales_accounts_lead_source
                ON sales_accounts(lead_source, cohort);
            CREATE TABLE IF NOT EXISTS account_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES sales_accounts(id) ON DELETE CASCADE,
                signals_json TEXT NOT NULL DEFAULT '[]',
                gates_json TEXT NOT NULL DEFAULT '[]',
                dimensions_json TEXT NOT NULL DEFAULT '[]',
                claim TEXT NOT NULL,
                epistemic_status TEXT NOT NULL,
                truth_state TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_url TEXT,
                observed_at TEXT,
                confidence TEXT NOT NULL,
                human_verified INTEGER NOT NULL DEFAULT 0,
                verified_by TEXT,
                verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_claims_account_recency
                ON account_claims(account_id, observed_at DESC, created_at DESC);
            """
        )
        email_columns = {
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(company_email_lookups)"
            )
        }
        if "email_verified" not in email_columns:
            self.connection.execute(
                "ALTER TABLE company_email_lookups ADD COLUMN "
                "email_verified INTEGER NOT NULL DEFAULT 0"
            )
        if "error_json" not in email_columns:
            self.connection.execute(
                "ALTER TABLE company_email_lookups ADD COLUMN "
                "error_json TEXT NOT NULL DEFAULT '{}'"
            )
        progress_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(run_progress)").fetchall()
        }
        if "enriched" not in progress_columns:
            self.connection.execute(
                "ALTER TABLE run_progress ADD COLUMN enriched INTEGER NOT NULL DEFAULT 0"
            )
        if "external_checked" not in progress_columns:
            self.connection.execute(
                "ALTER TABLE run_progress ADD COLUMN external_checked INTEGER NOT NULL DEFAULT 0"
            )
        candidate_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(candidates)").fetchall()
        }
        if "discovery_evidence_json" not in candidate_columns:
            self.connection.execute(
                "ALTER TABLE candidates ADD COLUMN "
                "discovery_evidence_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "provider_record_ids_json" not in candidate_columns:
            self.connection.execute(
                "ALTER TABLE candidates ADD COLUMN "
                "provider_record_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        target_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(snapshot_target_intelligence)"
            ).fetchall()
        }
        for column, definition in (
            ("durable_fit_score", "INTEGER NOT NULL DEFAULT 0"),
            ("contact_now_score", "INTEGER NOT NULL DEFAULT 0"),
            ("founder_priority_score", "INTEGER NOT NULL DEFAULT 0"),
            ("opportunity_risk", "INTEGER NOT NULL DEFAULT 0"),
            ("opportunity_uncertainty", "INTEGER NOT NULL DEFAULT 100"),
            ("recommended_action", "TEXT NOT NULL DEFAULT 'research_next'"),
        ):
            if column not in target_columns:
                self.connection.execute(
                    f"ALTER TABLE snapshot_target_intelligence ADD COLUMN {column} {definition}"
                )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshot_target_founder_priority
            ON snapshot_target_intelligence(
                relationship_lane, target_scope, live, disqualified,
                founder_priority_score DESC, contact_now_score DESC,
                durable_fit_score DESC, target_score DESC
            )
            """
        )
        cache_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(free_signal_cache)").fetchall()
        }
        if "cache_status" not in cache_columns:
            self.connection.execute(
                "ALTER TABLE free_signal_cache ADD COLUMN "
                "cache_status TEXT NOT NULL DEFAULT 'success'"
            )
        if "error_count" not in cache_columns:
            self.connection.execute(
                "ALTER TABLE free_signal_cache ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0"
            )
        suggestion_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(source_suggestions)").fetchall()
        }
        relationship_added = "relationship_type" not in suggestion_columns
        if relationship_added:
            self.connection.execute(
                "ALTER TABLE source_suggestions ADD COLUMN "
                "relationship_type TEXT NOT NULL DEFAULT 'buyer'"
            )
            # Old agency suggestions implicitly hydrated as referral paths. Preserve
            # that behavior exactly once while letting all new suggestions state
            # direct-buyer versus referral intent explicitly.
            self.connection.execute(
                """
                UPDATE source_suggestions
                SET relationship_type='referral_partner'
                WHERE source_kind='agency_directory' OR category='Agency'
                """
            )
        if "source_family" not in suggestion_columns:
            self.connection.execute(
                "ALTER TABLE source_suggestions ADD COLUMN "
                "source_family TEXT NOT NULL DEFAULT 'other'"
            )
            # Kind alone cannot safely classify a platform/customer index as a
            # competitor, ad-tech, or creative-operations ecosystem. Agency
            # directories are the sole conservative historical family inference.
            self.connection.execute(
                """
                UPDATE source_suggestions
                SET source_family='agency_delivery_capacity'
                WHERE source_kind='agency_directory'
                """
            )
        if "buyer_archetypes_json" not in suggestion_columns:
            self.connection.execute(
                "ALTER TABLE source_suggestions ADD COLUMN "
                "buyer_archetypes_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "source_identity_key" not in suggestion_columns:
            self.connection.execute(
                "ALTER TABLE source_suggestions ADD COLUMN "
                "source_identity_key TEXT NOT NULL DEFAULT ''"
            )
        if "duplicate_of_suggestion_id" not in suggestion_columns:
            self.connection.execute(
                "ALTER TABLE source_suggestions ADD COLUMN "
                "duplicate_of_suggestion_id INTEGER "
                "REFERENCES source_suggestions(id) ON DELETE RESTRICT"
            )
        # Install identity ownership before any source-suggestion reads can observe the
        # newly added empty defaults. This migration commits independently so it can use
        # one immediate transaction and leave no partially aliased catalog behind.
        self.connection.commit()
        self._initialize_source_suggestion_identities()
        # Existing candidate rows predate durable multi-source lineage. This is a
        # conservative seed: it preserves the currently recorded origin and lets all
        # future discoveries add independent edges without rewriting it.
        self.connection.execute(
            """
            INSERT OR IGNORE INTO candidate_source_edges(
                candidate_id, source_apex, discovery_rank, label_hint,
                evidence_urls_json, provider_record_ids_json,
                first_seen_at, last_seen_at
            )
            SELECT id, source_apex, discovery_rank, label_hint,
                   discovery_evidence_json, provider_record_ids_json,
                   discovered_at, last_discovered_at
            FROM candidates
            """
        )
        workflow_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(prospect_workflow)").fetchall()
        }
        if "next_follow_up_at" not in workflow_columns:
            self.connection.execute(
                "ALTER TABLE prospect_workflow ADD COLUMN next_follow_up_at TEXT"
            )
        if "excluded" not in workflow_columns:
            self.connection.execute(
                "ALTER TABLE prospect_workflow ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"
            )
        if "scan_enabled" not in workflow_columns:
            self.connection.execute(
                "ALTER TABLE prospect_workflow ADD COLUMN scan_enabled INTEGER NOT NULL DEFAULT 1"
            )
        company_scan_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(company_scan_work)").fetchall()
        }
        if "next_retry_at" not in company_scan_columns:
            self.connection.execute(
                "ALTER TABLE company_scan_work ADD COLUMN next_retry_at TEXT"
            )
        claim_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(account_claims)").fetchall()
        }
        if "gates_json" not in claim_columns:
            self.connection.execute(
                "ALTER TABLE account_claims ADD COLUMN gates_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "dimensions_json" not in claim_columns:
            self.connection.execute(
                "ALTER TABLE account_claims ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '[]'"
            )
        advertiser_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(advertiser_entities)").fetchall()
        }
        if "linked_instagram_handle" not in advertiser_columns:
            self.connection.execute(
                "ALTER TABLE advertiser_entities ADD COLUMN "
                "linked_instagram_handle TEXT NOT NULL DEFAULT ''"
            )
        ad_observation_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(candidate_ad_observations)"
            ).fetchall()
        }
        for column, definition in (
            ("video_creatives", "INTEGER NOT NULL DEFAULT 0"),
            ("image_creatives", "INTEGER NOT NULL DEFAULT 0"),
            ("coverage_notes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in ad_observation_columns:
                self.connection.execute(
                    f"ALTER TABLE candidate_ad_observations ADD COLUMN {column} {definition}"
                )
        self.connection.commit()
        self.connection.execute(COMPANY_SCAN_RUN_LEASE_INDEX_SQL)
        if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_company_scan_work_completed_candidate'"
        ).fetchone() is None:
            self.connection.execute(COMPANY_SCAN_COMPLETION_INDEX_SQL)
            # Give the OR lookup its true candidate selectivity immediately; otherwise
            # SQLite may prefer walking every completed company in date order.
            self.connection.execute("ANALYZE idx_company_scan_work_completed_candidate")
        self._initialize_frontier_scanner_persistence()
        self._initialize_market_feed_persistence()
        from .market_identity import initialize as initialize_market_identity

        initialize_market_identity(self.connection)
        self._reindex_snapshot_scores()
        self._backfill_snapshot_target_intelligence()
        self._backfill_company_scan_work()
        self._initialize_safe_recovery_index()
        self._repair_invalid_company_scan_work()
        from .buyer_intelligence import initialize_schema as initialize_buyer_evidence
        from .buyer_intelligence_collection import initialize_schema as initialize_buyer_collection
        from .sales_engine.ranking import initialize_schema

        initialize_buyer_evidence(self.connection)
        initialize_buyer_collection(self.connection)
        initialize_schema(self.connection)
        from .missions.store import initialize as initialize_missions

        initialize_missions(self.connection)
        from .spending import initialize as initialize_spending

        initialize_spending(self.connection)
        from .sales_engine.outbound import initialize as initialize_outbound

        initialize_outbound(self.connection)
        from .inventory_census import initialize as initialize_inventory_census

        initialize_inventory_census(self.connection)
        from .company_research import initialize as initialize_research_backoff

        initialize_research_backoff(self.connection)
        self.connection.execute("PRAGMA optimize")
        self.connection.commit()

    def _initialize_safe_recovery_index(self) -> None:
        """Upgrade the legacy partial index without evaluating malformed JSON."""
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_snapshots_ai_recovery'"
        ).fetchone()
        if row and "json_valid(observation_json)" in row[0]:
            return
        from .sales_engine.persistence import SalesEngineRepository

        with SalesEngineRepository(self.connection)._transaction():
            self.connection.execute("DROP INDEX IF EXISTS idx_snapshots_ai_recovery")
            self.connection.execute("""CREATE INDEX idx_snapshots_ai_recovery
                ON snapshots(candidate_id,id DESC)
                WHERE CASE WHEN json_valid(observation_json)
                    AND (ai_json IS NULL OR json_valid(ai_json)) THEN
                    COALESCE(json_extract(observation_json,'$.canonical_recovery_required'),0)=1
                    AND COALESCE(json_extract(ai_json,'$.company_url'),'')=''
                    ELSE 0 END""")

    def _initialize_source_suggestion_identities(self) -> None:
        """Backfill one operational owner per semantic source without deleting history."""

        index_statements = (
            """
            CREATE INDEX IF NOT EXISTS idx_source_suggestions_identity_members
            ON source_suggestions(source_identity_key, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_source_suggestions_duplicate_of
            ON source_suggestions(duplicate_of_suggestion_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_suggestions_one_canonical_identity
            ON source_suggestions(source_identity_key)
            WHERE duplicate_of_suggestion_id IS NULL AND source_identity_key<>''
            """,
        )
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (SOURCE_SUGGESTION_IDENTITY_MIGRATION,),
        ).fetchone()
        if applied is not None:
            for statement in index_statements:
                self.connection.execute(statement)
            self.connection.commit()
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """
                SELECT id, source_key, source_kind, apex, url, discovery_mechanism,
                       status
                FROM source_suggestions
                ORDER BY id
                """
            ).fetchall()
            historical_ids = {
                int(row[0])
                for row in self.connection.execute(
                    """
                    SELECT DISTINCT CAST(member.value AS INTEGER)
                    FROM source_execution_runs execution
                    JOIN json_each(execution.suggestion_ids_json) member
                    UNION
                    SELECT DISTINCT suggestion_id
                    FROM discovery_source_candidates
                    """
                ).fetchall()
            }
            grouped: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                identity = source_identity_key(
                    source_kind=str(row["source_kind"] or ""),
                    apex=str(row["apex"] or ""),
                    url=str(row["url"] or ""),
                    discovery_mechanism=str(row["discovery_mechanism"] or ""),
                    legacy_token=f"{int(row['id'])}:{str(row['source_key'] or '')}",
                )
                grouped.setdefault(identity, []).append(row)

            status_priority = {"accepted": 0, "rejected": 1, "pending": 2}
            alias_count = 0
            for identity, members in grouped.items():
                owner = min(
                    members,
                    key=lambda row: (
                        status_priority.get(str(row["status"]), 3),
                        0 if int(row["id"]) in historical_ids else 1,
                        int(row["id"]),
                    ),
                )
                owner_id = int(owner["id"])
                for member in members:
                    member_id = int(member["id"])
                    duplicate_of = None if member_id == owner_id else owner_id
                    alias_count += int(duplicate_of is not None)
                    self.connection.execute(
                        """
                        UPDATE source_suggestions
                        SET source_identity_key=?, duplicate_of_suggestion_id=?
                        WHERE id=?
                        """,
                        (identity, duplicate_of, member_id),
                    )

            for statement in index_statements:
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, ?)
                """,
                (
                    SOURCE_SUGGESTION_IDENTITY_MIGRATION,
                    _now(),
                    len(rows) + alias_count,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _initialize_frontier_scanner_persistence(self) -> None:
        """Install the additive scanner ledger as one idempotent migration."""
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (FRONTIER_SCANNER_PERSISTENCE_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        statements = (
            """
            CREATE TABLE IF NOT EXISTS scanner_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
                run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT '',
                scope_key TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN (
                    'running', 'success', 'success_empty', 'not_applicable',
                    'partial', 'transient_error', 'rate_limited',
                    'robots_blocked', 'identity_ambiguous', 'failed'
                )),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                next_due_at TEXT,
                request_count INTEGER NOT NULL DEFAULT 0,
                byte_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                cost_microusd INTEGER NOT NULL DEFAULT 0,
                artifact_count INTEGER NOT NULL DEFAULT 0,
                coverage_count INTEGER NOT NULL DEFAULT 0,
                observation_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_executions_company_time
            ON scanner_executions(
                company_key, scanner_id, scanner_version, started_at DESC, id DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_executions_status_due
            ON scanner_executions(scanner_id, scanner_version, status, next_due_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_executions_candidate_status
            ON scanner_executions(candidate_id, status)
            """,
            """
            CREATE TABLE IF NOT EXISTS scanner_work (
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'running', 'success', 'success_empty', 'not_applicable',
                    'partial', 'transient_error', 'rate_limited',
                    'robots_blocked', 'identity_ambiguous', 'failed'
                )),
                claimed_candidate_id INTEGER
                    REFERENCES candidates(id) ON DELETE SET NULL,
                claimed_execution_id INTEGER
                    REFERENCES scanner_executions(id) ON DELETE SET NULL,
                claimed_at TEXT,
                lease_expires_at TEXT,
                completed_candidate_id INTEGER
                    REFERENCES candidates(id) ON DELETE SET NULL,
                completed_execution_id INTEGER
                    REFERENCES scanner_executions(id) ON DELETE SET NULL,
                completed_at TEXT,
                next_due_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(company_key, scanner_id, scanner_version)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_work_active_lease
            ON scanner_work(scanner_id, scanner_version, status, lease_expires_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_work_completion
            ON scanner_work(
                scanner_id, scanner_version, status, completed_at DESC, next_due_at
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS coverage_manifests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL
                    REFERENCES scanner_executions(id) ON DELETE RESTRICT,
                manifest_key TEXT NOT NULL,
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                surface TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                window_start TEXT,
                window_end TEXT,
                attempted_units INTEGER NOT NULL DEFAULT 0,
                successful_units INTEGER NOT NULL DEFAULT 0,
                failed_units INTEGER NOT NULL DEFAULT 0,
                total_units INTEGER,
                completeness TEXT NOT NULL CHECK(completeness IN (
                    'none', 'partial', 'complete', 'not_applicable'
                )),
                truncated INTEGER NOT NULL DEFAULT 0,
                absence_interpretable INTEGER NOT NULL DEFAULT 0,
                limitations_json TEXT NOT NULL DEFAULT '[]',
                source_urls_json TEXT NOT NULL DEFAULT '[]',
                observed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(execution_id, manifest_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_coverage_manifests_company_time
            ON coverage_manifests(company_key, scanner_id, observed_at DESC, id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS evidence_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL
                    REFERENCES scanner_executions(id) ON DELETE RESTRICT,
                artifact_key TEXT NOT NULL,
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT '',
                artifact_type TEXT NOT NULL,
                canonical_url TEXT NOT NULL DEFAULT '',
                original_url TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL,
                status_code INTEGER,
                mime_type TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                published_time_basis TEXT NOT NULL,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                byte_count INTEGER NOT NULL DEFAULT 0,
                robots_state TEXT NOT NULL,
                truncated INTEGER NOT NULL DEFAULT 0,
                excerpt TEXT NOT NULL DEFAULT '',
                locator TEXT NOT NULL DEFAULT '',
                retention_policy TEXT NOT NULL,
                license_policy TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(execution_id, artifact_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_company_time
            ON evidence_artifacts(company_key, scanner_id, fetched_at DESC, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_content_hash
            ON evidence_artifacts(content_hash) WHERE content_hash<>''
            """,
            """
            CREATE TABLE IF NOT EXISTS atomic_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL
                    REFERENCES scanner_executions(id) ON DELETE RESTRICT,
                observation_key TEXT NOT NULL,
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT '',
                rule_id TEXT NOT NULL DEFAULT '',
                rule_version TEXT NOT NULL DEFAULT '',
                entity_key TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value_type TEXT NOT NULL,
                value_json TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                qualifiers_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                epistemic_status TEXT NOT NULL,
                validity_status TEXT NOT NULL,
                confidence TEXT NOT NULL,
                confidence_method TEXT NOT NULL DEFAULT '',
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                effective_at TEXT,
                effective_time_basis TEXT NOT NULL,
                expires_at TEXT,
                artifact_keys_json TEXT NOT NULL DEFAULT '[]',
                coverage_keys_json TEXT NOT NULL DEFAULT '[]',
                score_eligible INTEGER NOT NULL DEFAULT 0,
                discovery_only INTEGER NOT NULL DEFAULT 0,
                source_actor_key TEXT NOT NULL DEFAULT '',
                origin_fingerprint TEXT NOT NULL DEFAULT '',
                causal_root_key TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(execution_id, observation_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_atomic_observations_company_asof
            ON atomic_observations(
                company_key, recorded_at, first_observed_at, scanner_id, predicate
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_atomic_observations_predicate_time
            ON atomic_observations(predicate, effective_at DESC, recorded_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_atomic_observations_origin
            ON atomic_observations(company_key, causal_root_key, origin_fingerprint)
            """,
            """
            CREATE TABLE IF NOT EXISTS observation_lineage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL
                    REFERENCES scanner_executions(id) ON DELETE RESTRICT,
                subject_observation_key TEXT NOT NULL,
                relationship_type TEXT NOT NULL CHECK(relationship_type IN (
                    'extracted_from', 'corroborates', 'contradicts',
                    'syndicated_from', 'derived_from'
                )),
                object_observation_key TEXT,
                artifact_key TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                CHECK(object_observation_key IS NOT NULL OR artifact_key IS NOT NULL)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_observation_lineage_subject
            ON observation_lineage(subject_observation_key, relationship_type)
            """,
            """
            CREATE TABLE IF NOT EXISTS scanner_schedule_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
                execution_id INTEGER
                    REFERENCES scanner_executions(id) ON DELETE SET NULL,
                company_key TEXT NOT NULL,
                scanner_id TEXT NOT NULL,
                scanner_version TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                applicable INTEGER NOT NULL,
                applicability_reason TEXT NOT NULL DEFAULT '',
                current_action TEXT NOT NULL DEFAULT '',
                success_probability REAL NOT NULL DEFAULT 0,
                expected_action_entropy_reduction REAL NOT NULL DEFAULT 0,
                action_boundary_sensitivity REAL NOT NULL DEFAULT 0,
                evidence_value REAL NOT NULL DEFAULT 0,
                cost_penalty REAL NOT NULL DEFAULT 0,
                rate_penalty REAL NOT NULL DEFAULT 0,
                identity_penalty REAL NOT NULL DEFAULT 0,
                risk_penalty REAL NOT NULL DEFAULT 0,
                expected_information_gain REAL NOT NULL DEFAULT 0,
                selected INTEGER NOT NULL DEFAULT 0,
                selection_reason TEXT NOT NULL DEFAULT '',
                exploration INTEGER NOT NULL DEFAULT 0,
                propensity REAL NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scanner_schedule_company_time
            ON scanner_schedule_decisions(company_key, decided_at DESC, id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS intelligence_feature_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_key TEXT NOT NULL,
                as_of_at TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                features_json TEXT NOT NULL DEFAULT '{}',
                missingness_json TEXT NOT NULL DEFAULT '{}',
                coverage_json TEXT NOT NULL DEFAULT '{}',
                scanner_states_json TEXT NOT NULL DEFAULT '{}',
                evidence_ages_json TEXT NOT NULL DEFAULT '{}',
                scheduling_propensity REAL,
                action_shown TEXT NOT NULL DEFAULT '',
                observation_keys_json TEXT NOT NULL DEFAULT '[]',
                recorded_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_feature_snapshots_company_asof
            ON intelligence_feature_snapshots(
                company_key, as_of_at DESC, feature_version, id DESC
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS scanner_executions_no_delete
            BEFORE DELETE ON scanner_executions
            BEGIN
                SELECT RAISE(ABORT, 'scanner executions are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS scanner_executions_terminal_immutable
            BEFORE UPDATE ON scanner_executions
            WHEN OLD.status<>'running'
            BEGIN
                SELECT RAISE(ABORT, 'terminal scanner executions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS scanner_executions_identity_immutable
            BEFORE UPDATE ON scanner_executions
            WHEN OLD.candidate_id IS NOT NEW.candidate_id
              OR OLD.run_id IS NOT NEW.run_id
              OR OLD.company_key IS NOT NEW.company_key
              OR OLD.scanner_id IS NOT NEW.scanner_id
              OR OLD.scanner_version IS NOT NEW.scanner_version
              OR OLD.extractor_version IS NOT NEW.extractor_version
              OR OLD.scope_key IS NOT NEW.scope_key
              OR OLD.config_hash IS NOT NEW.config_hash
              OR OLD.started_at IS NOT NEW.started_at
              OR OLD.recorded_at IS NOT NEW.recorded_at
            BEGIN
                SELECT RAISE(ABORT, 'scanner execution identity is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS coverage_manifests_no_update
            BEFORE UPDATE ON coverage_manifests
            BEGIN
                SELECT RAISE(ABORT, 'coverage manifests are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS coverage_manifests_no_delete
            BEFORE DELETE ON coverage_manifests
            BEGIN
                SELECT RAISE(ABORT, 'coverage manifests are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS evidence_artifacts_no_update
            BEFORE UPDATE ON evidence_artifacts
            BEGIN
                SELECT RAISE(ABORT, 'evidence artifacts are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS evidence_artifacts_no_delete
            BEFORE DELETE ON evidence_artifacts
            BEGIN
                SELECT RAISE(ABORT, 'evidence artifacts are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS atomic_observations_no_update
            BEFORE UPDATE ON atomic_observations
            BEGIN
                SELECT RAISE(ABORT, 'atomic observations are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS atomic_observations_no_delete
            BEFORE DELETE ON atomic_observations
            BEGIN
                SELECT RAISE(ABORT, 'atomic observations are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS observation_lineage_no_update
            BEFORE UPDATE ON observation_lineage
            BEGIN
                SELECT RAISE(ABORT, 'observation lineage is append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS observation_lineage_no_delete
            BEFORE DELETE ON observation_lineage
            BEGIN
                SELECT RAISE(ABORT, 'observation lineage is append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS scanner_schedule_decisions_no_update
            BEFORE UPDATE ON scanner_schedule_decisions
            BEGIN
                SELECT RAISE(ABORT, 'scanner schedule decisions are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS scanner_schedule_decisions_no_delete
            BEFORE DELETE ON scanner_schedule_decisions
            BEGIN
                SELECT RAISE(ABORT, 'scanner schedule decisions are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS intelligence_feature_snapshots_no_update
            BEFORE UPDATE ON intelligence_feature_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'feature snapshots are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS intelligence_feature_snapshots_no_delete
            BEFORE DELETE ON intelligence_feature_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'feature snapshots are append-only');
            END
            """,
        )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                (FRONTIER_SCANNER_PERSISTENCE_MIGRATION,),
            ).fetchone()
            if applied is not None:
                self.connection.commit()
                return
            for table, column in (
                ("candidates", "discovery_assertions_json"),
                ("candidate_source_edges", "discovery_assertions_json"),
            ):
                columns = {
                    str(row["name"])
                    for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
                    )
            for statement in statements:
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, 0)
                """,
                (FRONTIER_SCANNER_PERSISTENCE_MIGRATION, _now()),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _initialize_market_feed_persistence(self) -> None:
        """Install the additive market-feed ledger without rewriting existing evidence."""
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (MARKET_FEED_PERSISTENCE_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        statements = (
            """
            CREATE TABLE IF NOT EXISTS market_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                source_external_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                revision_key TEXT NOT NULL,
                canonical_url TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                source_updated_at TEXT,
                observed_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                supersedes_document_id INTEGER
                    REFERENCES market_documents(id) ON DELETE RESTRICT,
                causal_root_key TEXT NOT NULL DEFAULT '',
                source_policy_version TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(source_id, revision_key),
                UNIQUE(source_id, source_external_id, content_hash),
                UNIQUE(source_id, canonical_key, revision)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_documents_source_external
            ON market_documents(source_id, source_external_id, revision DESC, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_documents_source_published
            ON market_documents(source_id, published_at DESC, observed_at DESC, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_documents_canonical_latest
            ON market_documents(source_id, canonical_key, revision DESC, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_documents_causal_root
            ON market_documents(causal_root_key, published_at DESC)
            WHERE causal_root_key<>''
            """,
            """
            CREATE TABLE IF NOT EXISTS market_entity_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES market_documents(id) ON DELETE RESTRICT,
                mention_key TEXT NOT NULL,
                raw_name TEXT NOT NULL DEFAULT '',
                normalized_name TEXT NOT NULL DEFAULT '',
                entity_role TEXT NOT NULL DEFAULT 'unknown',
                context_excerpt TEXT NOT NULL DEFAULT '',
                identifiers_json TEXT NOT NULL DEFAULT '{}',
                candidate_domain TEXT NOT NULL DEFAULT '',
                promotable INTEGER NOT NULL DEFAULT 0,
                confidence TEXT NOT NULL DEFAULT 'low',
                resolution_status TEXT NOT NULL DEFAULT 'unresolved'
                    CHECK(resolution_status IN ('unresolved', 'ambiguous', 'resolved')),
                resolution_method TEXT NOT NULL DEFAULT '',
                resolution_confidence TEXT NOT NULL DEFAULT 'low',
                candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
                company_key TEXT,
                resolution_notes TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                supersedes_mention_id INTEGER
                    REFERENCES market_entity_mentions(id) ON DELETE RESTRICT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(document_id, mention_key),
                CHECK(
                    (resolution_status='resolved' AND company_key IS NOT NULL)
                    OR (
                        resolution_status<>'resolved'
                        AND candidate_id IS NULL
                        AND company_key IS NULL
                    )
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_mentions_unresolved
            ON market_entity_mentions(resolution_status, normalized_name, id)
            WHERE resolution_status<>'resolved'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_mentions_resolved_company
            ON market_entity_mentions(company_key, updated_at DESC, id DESC)
            WHERE resolution_status='resolved'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_mentions_candidate
            ON market_entity_mentions(candidate_id, updated_at DESC, id DESC)
            WHERE candidate_id IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS market_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES market_documents(id) ON DELETE RESTRICT,
                mention_id INTEGER
                    REFERENCES market_entity_mentions(id) ON DELETE RESTRICT,
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                assertion_mode TEXT NOT NULL DEFAULT 'reported',
                subject_role TEXT NOT NULL DEFAULT 'subject',
                effective_at TEXT,
                announced_at TEXT,
                amount_json TEXT NOT NULL DEFAULT 'null',
                amount_currency TEXT NOT NULL DEFAULT '',
                amount_semantics TEXT NOT NULL DEFAULT '',
                causal_root_key TEXT NOT NULL DEFAULT '',
                independence_cluster TEXT NOT NULL DEFAULT '',
                corroboration_json TEXT NOT NULL DEFAULT '{}',
                confidence TEXT NOT NULL DEFAULT 'low',
                summary TEXT NOT NULL DEFAULT '',
                direct_candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
                direct_company_key TEXT,
                semantic_eligible INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                supersedes_event_id INTEGER
                    REFERENCES market_events(id) ON DELETE RESTRICT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(document_id, event_key),
                CHECK(
                    (direct_candidate_id IS NULL AND direct_company_key IS NULL)
                    OR direct_company_key IS NOT NULL
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_events_type_effective
            ON market_events(event_type, effective_at DESC, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_events_causal_root
            ON market_events(causal_root_key, effective_at DESC, id DESC)
            WHERE causal_root_key<>''
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_events_independence
            ON market_events(independence_cluster, effective_at DESC, id DESC)
            WHERE independence_cluster<>''
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_events_direct_company
            ON market_events(direct_company_key, effective_at DESC, id DESC)
            WHERE direct_company_key IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_events_mention
            ON market_events(mention_id, effective_at DESC, id DESC)
            WHERE mention_id IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS market_source_cursors (
                source_id TEXT PRIMARY KEY,
                source_policy_version TEXT NOT NULL DEFAULT '',
                cursor_json TEXT NOT NULL DEFAULT '{}',
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                last_polled_at TEXT NOT NULL,
                last_success_at TEXT NOT NULL,
                latest_document_at TEXT,
                latest_published_at TEXT,
                documents_persisted INTEGER NOT NULL DEFAULT 0,
                events_persisted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_market_source_cursors_recency
            ON market_source_cursors(last_success_at, source_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_documents_no_update
            BEFORE UPDATE ON market_documents
            BEGIN
                SELECT RAISE(ABORT, 'market documents are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_documents_no_delete
            BEFORE DELETE ON market_documents
            BEGIN
                SELECT RAISE(ABORT, 'market documents are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_mentions_extraction_immutable
            BEFORE UPDATE ON market_entity_mentions
            WHEN OLD.document_id IS NOT NEW.document_id
              OR OLD.mention_key IS NOT NEW.mention_key
              OR OLD.raw_name IS NOT NEW.raw_name
              OR OLD.normalized_name IS NOT NEW.normalized_name
              OR OLD.entity_role IS NOT NEW.entity_role
              OR OLD.context_excerpt IS NOT NEW.context_excerpt
              OR OLD.identifiers_json IS NOT NEW.identifiers_json
              OR OLD.candidate_domain IS NOT NEW.candidate_domain
              OR OLD.promotable IS NOT NEW.promotable
              OR OLD.confidence IS NOT NEW.confidence
              OR OLD.revision IS NOT NEW.revision
              OR OLD.supersedes_mention_id IS NOT NEW.supersedes_mention_id
              OR OLD.metadata_json IS NOT NEW.metadata_json
              OR OLD.recorded_at IS NOT NEW.recorded_at
            BEGIN
                SELECT RAISE(ABORT, 'market mention extraction is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_mentions_no_delete
            BEFORE DELETE ON market_entity_mentions
            BEGIN
                SELECT RAISE(ABORT, 'market mentions cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_events_no_update
            BEFORE UPDATE ON market_events
            BEGIN
                SELECT RAISE(ABORT, 'market events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS market_events_no_delete
            BEFORE DELETE ON market_events
            BEGIN
                SELECT RAISE(ABORT, 'market events are append-only');
            END
            """,
        )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                (MARKET_FEED_PERSISTENCE_MIGRATION,),
            ).fetchone()
            if applied is not None:
                self.connection.commit()
                return
            for statement in statements:
                self.connection.execute(statement)
            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, 0)
                """,
                (MARKET_FEED_PERSISTENCE_MIGRATION, _now()),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _reindex_snapshot_scores(self) -> None:
        """Re-score legacy snapshots with current public-evidence safety rules.

        The migration is local and uses only persisted observations. It performs no network or
        AI work, preserves observations/AI/final scores/change history, and records completion
        so normal request-scoped LeadStore construction stays constant-time.
        """
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (SNAPSHOT_SCORE_REINDEX,),
        ).fetchone()
        if applied is not None:
            return

        self.connection.execute("BEGIN IMMEDIATE")
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (SNAPSHOT_SCORE_REINDEX,),
        ).fetchone()
        if applied is not None:
            self.connection.commit()
            return

        # Imported lazily to keep the persistence module's import boundary acyclic.
        from .scoring import score_candidate

        rows_changed = 0
        last_snapshot_id = 0
        previous_observations: dict[int, SiteObservation] = {}
        while True:
            rows = self.connection.execute(
                """
                SELECT s.id, s.candidate_id, s.observation_json, s.score_json,
                       c.source_apex, c.hostname, c.discovery_assertions_json
                FROM snapshots s
                JOIN candidates c ON c.id=s.candidate_id
                WHERE s.id>?
                ORDER BY s.id
                LIMIT 250
                """,
                (last_snapshot_id,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_snapshot_id = int(row["id"])
                try:
                    observation = SiteObservation.model_validate_json(row["observation_json"])
                    candidate = DiscoveredHost(
                        source_apex=row["source_apex"],
                        hostname=row["hostname"],
                        discovery_assertions=json.loads(
                            row["discovery_assertions_json"] or "[]"
                        ),
                    )
                    score = score_candidate(
                        candidate,
                        observation,
                        reference_time=(self._datetime_or_none(
                            json.loads(row["score_json"]).get("creative_capacity", {})
                            .get("opportunity", {}).get("freshness_audit", {}).get("as_of")
                        ) or observation.observed_at),
                        previous_observation=previous_observations.get(int(row["candidate_id"])),
                    )
                except (AttributeError, TypeError, ValueError):
                    # A corrupt legacy row must not make the local application unopenable.
                    continue
                previous_observations[int(row["candidate_id"])] = observation
                self.connection.execute(
                    "UPDATE snapshots SET score_json=? WHERE id=?",
                    (encode_storage_json(score.model_dump_json(), "score"), row["id"]),
                )
                rows_changed += 1

        self.connection.execute(
            """
            INSERT INTO schema_migrations(name, applied_at, rows_changed)
            VALUES (?, ?, ?)
            """,
            (SNAPSHOT_SCORE_REINDEX, _now(), rows_changed),
        )
        self.connection.commit()

    @staticmethod
    def _normalize_identity_host(value: str | None) -> str:
        raw = (value or "").strip().lower().rstrip(".")
        if not raw:
            return ""
        try:
            if "://" in raw:
                raw = urlsplit(raw).hostname or ""
            else:
                raw = raw.split("/", 1)[0].split(":", 1)[0]
            host = raw.encode("idna").decode("ascii").lower().rstrip(".")
        except (ValueError, UnicodeError):
            return ""
        return host.removeprefix("www.")

    @classmethod
    def _appearance_subject_domain(cls, value: str | None) -> str:
        """Return a defensible hostname for a displayed company, or abstain."""

        host = cls._normalize_identity_host(value)
        labels = host.split(".")
        if len(labels) < 2 or labels[-1].isdigit():
            return ""
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        ):
            return ""
        return host

    @staticmethod
    def _unresolved_appearance_key(value: str) -> str:
        """Build a stable opaque key without pretending a displayed name is a domain."""

        normalized = re.sub(r"[\W_]+", " ", value.casefold(), flags=re.UNICODE).strip()
        if not normalized:
            return ""
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"unresolved:{identity}"

    @staticmethod
    def _appearance_payload_identity(payload: dict[str, Any]) -> tuple[str, ...]:
        """Identify one independently observed appearance inside a relationship edge."""

        return tuple(
            str(payload.get(field) or "")
            for field in (
                "relation_type",
                "context_hash",
                "asset_url",
                "extraction_method",
                "subject_url",
                "subject_domain",
                "evidence_kind",
            )
        )

    @classmethod
    def _merge_appearance_payloads(cls, *payloads: dict[str, Any]) -> dict[str, Any]:
        """Retain bounded appearance provenance when the graph edge is observed repeatedly."""

        merged: dict[tuple[str, ...], dict[str, Any]] = {}
        for payload in payloads:
            raw_evidence = payload.get("evidence")
            evidence_items = raw_evidence if isinstance(raw_evidence, list) else [payload]
            for raw in evidence_items:
                if not isinstance(raw, dict):
                    continue
                evidence = {key: value for key, value in raw.items() if key != "evidence"}
                identity = cls._appearance_payload_identity(evidence)
                current = merged.get(identity)
                if current is None:
                    observed_at = str(evidence.get("observed_at") or "")
                    evidence["last_observed_at"] = str(
                        evidence.get("last_observed_at") or observed_at
                    )
                    merged[identity] = evidence
                    continue
                first_observed = str(
                    current.get("observed_at") or evidence.get("observed_at") or ""
                )
                last_observed = str(
                    evidence.get("last_observed_at")
                    or evidence.get("observed_at")
                    or current.get("last_observed_at")
                    or first_observed
                )
                merged[identity] = {
                    **current,
                    **evidence,
                    "observed_at": first_observed,
                    "last_observed_at": last_observed,
                }

        evidence = list(merged.values())[-30:]
        if not evidence:
            return {}
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        canonical = max(
            evidence,
            key=lambda item: (
                confidence_rank.get(str(item.get("identity_confidence") or "low"), 0),
                confidence_rank.get(str(item.get("confidence") or "low"), 0),
                bool(item.get("subject_domain")),
                bool(item.get("asset_url")),
                str(item.get("last_observed_at") or ""),
            ),
        )
        return {**canonical, "evidence": evidence}

    @classmethod
    def _target_canonical_domain(
        cls,
        candidate: DiscoveredHost,
        observation: SiteObservation,
    ) -> str:
        """Resolve current company identity without collapsing tenants to a platform apex."""
        source_apex = cls._normalize_identity_host(candidate.source_apex)
        fallback = cls._appearance_subject_domain(candidate.hostname)
        # Retained redactions and malformed canonical tags are not company identities.
        # Resolve lazily so a valid canonical host needs no final-URL parsing at all.
        for value in (observation.canonical_host, observation.final_url):
            host = cls._appearance_subject_domain(value)
            if not host or host == source_apex:
                continue
            return host
        return fallback

    @staticmethod
    def _target_lane_and_scope(candidate: DiscoveredHost, score: ScoreCard) -> tuple[str, str]:
        role = score.company_role.role
        if role == "directory_or_awards":
            return "referral_partner", "out_of_scope"
        if role == "software_vendor":
            return "buyer", "adjacency"
        if role in {"consumer_brand", "consumer_app_game"}:
            return "buyer", "direct"
        profile = source_hydration_profile(candidate.source_apex)
        profile_lane = profile.relationship_lane
        profile_scope = profile.target_scope
        if score.archetype == "agency":
            hypothesis = getattr(score, "creative_capacity", None)
            supported_procurement = (
                role == "production_agency"
                and "agency_buyer_candidate" in getattr(hypothesis, "derived_signals", ())
            )
            if (score.placement.ready or supported_procurement) and not score.disqualified:
                return "buyer", "direct"
            if role == "production_agency":
                # A supported operating role outweighs a discovery-source prior.
                # Procurement remains unverified, so this is still referral-only.
                return "referral_partner", "direct"
            return "referral_partner", (
                profile_scope if profile_scope in TARGET_SCOPES else "direct"
            )
        if score.archetype in {"ecommerce_brand", "media_membership"}:
            return "buyer", "direct"
        if score.archetype == "saas_or_other":
            return "buyer", "adjacency"
        if score.archetype == "creator_education":
            return "buyer", "out_of_scope"
        lane = profile_lane if profile_lane in TARGET_RELATIONSHIP_LANES else "buyer"
        scope = profile_scope if profile_scope in TARGET_SCOPES else "unclassified"
        return lane, scope

    def _upsert_snapshot_target_intelligence_uncommitted(
        self,
        *,
        snapshot_id: int,
        candidate_id: int,
        candidate: DiscoveredHost,
        observation: SiteObservation,
        score: ScoreCard,
        computed_at: str | None = None,
        writer=None,
    ) -> None:
        writer = self.connection if writer is None else writer
        hypothesis = CreativeCapacityHypothesis.model_validate(score.creative_capacity.model_dump())
        hypothesis.company_role = score.company_role.model_dump(mode="json")
        signal_values = hypothesis.principal_signals.model_dump().values()
        principal_signals_met = sum(value["truth_state"] == "met" for value in signal_values)
        relationship_lane, target_scope = self._target_lane_and_scope(candidate, score)
        opportunity = hypothesis.opportunity
        action = hypothesis.decision_packet.action
        company_key = self._target_canonical_domain(candidate, observation)
        writer.execute(
            TARGET_PROJECTION_SQL,
            (
                snapshot_id,
                candidate_id,
                hypothesis.version,
                company_key,
                relationship_lane,
                target_scope,
                score.archetype,
                hypothesis.total,
                principal_signals_met,
                int(hypothesis.review_ready),
                score.data_confidence,
                opportunity.durable_fit,
                opportunity.contact_now,
                opportunity.founder_priority,
                opportunity.risk,
                opportunity.uncertainty,
                action,
                int(observation.live),
                int(score.disqualified),
                encode_storage_json(hypothesis.model_dump_json(), "target"),
                computed_at or _now(),
            ),
        )
        self._project_signal_events_uncommitted(
            snapshot_id=snapshot_id,
            candidate_id=candidate_id,
            company_key=company_key,
            hypothesis=hypothesis,
            writer=writer,
        )
        self._project_company_relationships_uncommitted(
            candidate=candidate,
            observation=observation,
            score=score,
            company_key=company_key,
            writer=writer,
        )

    def _project_signal_events_uncommitted(
        self,
        *,
        snapshot_id: int,
        candidate_id: int,
        company_key: str,
        hypothesis: CreativeCapacityHypothesis,
        writer=None,
    ) -> None:
        """Replace one rebuildable event projection without touching snapshot truth."""

        writer = self.connection if writer is None else writer
        writer.execute(DELETE_PROJECTED_EVENTS_SQL, (snapshot_id,))
        rows = []
        for event in hypothesis.signal_events:
            rows.append(
                (
                    snapshot_id,
                    candidate_id,
                    company_key,
                    event.event_key,
                    event.event_type,
                    event.family,
                    event.polarity,
                    event.strength,
                    event.current_strength,
                    event.confidence,
                    event.coverage,
                    event.epistemic_status,
                    event.independence_key,
                    event.source_kind,
                    event.source_url,
                    event.observed_at.isoformat() if event.observed_at else None,
                    event.effective_at.isoformat() if event.effective_at else None,
                    event.expires_at.isoformat() if event.expires_at else None,
                    event.half_life_days,
                    event.summary,
                    json.dumps(event.details, sort_keys=True),
                )
            )
        if rows:
            writer.executemany(
                PROJECTED_EVENTS_SQL,
                rows,
            )

    def _appearance_relationship_rows_uncommitted(
        self,
        appearances: list[CompanyAppearanceEvidence],
        *,
        company_key: str,
        observed_at: str,
    ) -> list[tuple[Any, ...]]:
        """Project displayed companies without upgrading public proof to a current relationship."""

        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for appearance in appearances[:100]:
            assertion = appearance.assertion
            relation = str(assertion.relation_type).strip().lower() or "unknown"
            relationship_type = f"public_{relation}_appearance"
            source_url = assertion.source_url.strip()
            if not source_url:
                continue

            subject_name = appearance.subject_name.strip()
            subject_domain = self._appearance_subject_domain(appearance.subject_domain)
            if subject_domain:
                if subject_domain == company_key:
                    continue
                object_key = subject_domain
                object_type = "company"
            else:
                object_key = self._unresolved_appearance_key(subject_name)
                object_type = "unresolved_company"
            if not object_key:
                continue

            asset_url = (appearance.asset_url or assertion.asset_url).strip()
            payload = {
                "publisher_domain": assertion.publisher_domain,
                "relation_type": relation,
                "source_url": source_url,
                "subject_name": subject_name,
                "subject_domain": subject_domain,
                "subject_url": appearance.subject_url,
                "asset_url": asset_url,
                "assertion_asset_url": assertion.asset_url,
                "extraction_method": appearance.extraction_method,
                "evidence_kind": assertion.evidence_kind,
                "identity_confidence": appearance.identity_confidence,
                "confidence": assertion.confidence,
                "context_excerpt": assertion.context_excerpt,
                "context_hash": assertion.context_hash,
                "published_at": (
                    assertion.published_at.isoformat() if assertion.published_at else None
                ),
                "observed_at": observed_at,
                "last_observed_at": observed_at,
                # A logo, testimonial, or case-study appearance cannot establish that the
                # relationship is still active, even if a malformed upstream payload says so.
                "current_relationship_unverified": True,
            }
            key = (relationship_type, object_key, source_url)
            current = edges.get(key)
            if current is None:
                edges[key] = {
                    "object_type": object_type,
                    "object_name": subject_name or subject_domain,
                    "confidence": assertion.confidence,
                    "payload": self._merge_appearance_payloads(payload),
                }
                continue

            current["payload"] = self._merge_appearance_payloads(current["payload"], payload)
            if confidence_rank[assertion.confidence] > confidence_rank[str(current["confidence"])]:
                current["confidence"] = assertion.confidence
            canonical_name = str(current["payload"].get("subject_name") or "")
            current["object_name"] = canonical_name or str(current["object_name"])

        if not edges:
            return []

        existing_rows = self.connection.execute(
            """
            SELECT relationship_type, object_key, source_url, confidence, payload_json
            FROM company_relationships
            WHERE subject_key=?
            """,
            (company_key,),
        ).fetchall()
        for existing in existing_rows:
            relationship_type = str(existing["relationship_type"])
            if not (
                relationship_type.startswith("public_")
                and relationship_type.endswith("_appearance")
            ):
                continue
            key = (
                relationship_type,
                str(existing["object_key"]),
                str(existing["source_url"]),
            )
            edge = edges.get(key)
            if edge is None:
                continue
            try:
                previous_payload = json.loads(existing["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                previous_payload = {}
            edge["payload"] = self._merge_appearance_payloads(previous_payload, edge["payload"])
            old_confidence = str(existing["confidence"] or "low")
            if confidence_rank.get(old_confidence, 0) > confidence_rank.get(
                str(edge["confidence"]), 0
            ):
                edge["confidence"] = old_confidence
            canonical_name = str(edge["payload"].get("subject_name") or "")
            edge["object_name"] = canonical_name or str(edge["object_name"])

        return [
            (
                company_key,
                "company",
                relationship_type,
                object_key,
                str(edge["object_type"]),
                str(edge["object_name"]),
                source_url,
                str(edge["confidence"]),
                observed_at,
                observed_at,
                json.dumps(edge["payload"], sort_keys=True),
            )
            for (relationship_type, object_key, source_url), edge in edges.items()
        ]

    def _project_company_relationships_uncommitted(
        self,
        *,
        candidate: DiscoveredHost,
        observation: SiteObservation,
        score: ScoreCard,
        company_key: str,
        writer=None,
    ) -> None:
        """Upsert conservative temporal company/person/portfolio relationship evidence."""
        writer = self.connection if writer is None else writer

        observed_at = observation.observed_at.isoformat()
        rows: list[tuple[Any, ...]] = []
        alias = self._normalize_identity_host(candidate.hostname)
        alias_source = observation.final_url or f"https://{candidate.hostname}/"
        if alias and alias != company_key:
            rows.append(
                (
                    company_key,
                    "company",
                    "domain_alias",
                    alias,
                    "domain",
                    alias,
                    alias_source,
                    "high",
                    observed_at,
                    observed_at,
                    json.dumps({"candidate_source": candidate.source_apex}, sort_keys=True),
                )
            )
        for person in observation.contacts.people:
            if not person.name or not person.role or not person.source_url:
                continue
            identity = hashlib.sha256(
                f"{person.name.casefold()}|{person.role.casefold()}".encode()
            ).hexdigest()[:24]
            rows.append(
                (
                    company_key,
                    "company",
                    "public_person_role",
                    f"person:{identity}",
                    "person",
                    person.name,
                    person.source_url,
                    person.confidence,
                    (person.observed_at or observation.observed_at).isoformat(),
                    (person.observed_at or observation.observed_at).isoformat(),
                    json.dumps(
                        {
                            "role": person.role,
                            "buyer_role": person.buyer_role,
                            "profile_url": person.profile_url or "",
                            "method": person.method,
                        },
                        sort_keys=True,
                    ),
                )
            )
        rows.extend(
            self._appearance_relationship_rows_uncommitted(
                observation.web.company_appearances,
                company_key=company_key,
                observed_at=observed_at,
            )
        )
        if score.archetype == "agency":
            portfolio_source = next(
                (
                    url
                    for url in [*observation.web.source_urls, *observation.evidence_urls]
                    if any(
                        token in url.lower() for token in ("work", "client", "case", "portfolio")
                    )
                ),
                observation.final_url or f"https://{candidate.hostname}/",
            )
            agency_portfolio_domains = legacy_agency_portfolio_domains(observation.web)
            for domain in agency_portfolio_domains[:100]:
                target = self._normalize_identity_host(domain)
                if not target or target == company_key:
                    continue
                rows.append(
                    (
                        company_key,
                        "company",
                        "portfolio_reference",
                        target,
                        "company",
                        target,
                        portfolio_source,
                        "medium" if len(agency_portfolio_domains) >= 3 else "low",
                        observed_at,
                        observed_at,
                        json.dumps({"current_relationship_unverified": True}, sort_keys=True),
                    )
                )
        if not rows:
            return
        writer.executemany(
            PROJECTED_RELATIONSHIPS_SQL,
            rows,
        )

    def _backfill_snapshot_target_intelligence(self) -> None:
        """Build current-rubric projections without rewriting historical scan truth."""
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (TARGET_INTELLIGENCE_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                (TARGET_INTELLIGENCE_MIGRATION,),
            ).fetchone()
            if applied is not None:
                self.connection.commit()
                return

            # Imported lazily to preserve the persistence/scoring import boundary.
            from .scoring import score_candidate

            rows = self.connection.execute(
                """
                SELECT s.id AS snapshot_id, s.candidate_id, s.observation_json,
                       c.source_apex, c.hostname, c.first_seen, c.discovery_rank,
                       c.label_hint, c.discovery_evidence_json,
                       c.provider_record_ids_json, c.discovery_assertions_json
                FROM snapshots s
                JOIN candidates c ON c.id=s.candidate_id
                ORDER BY s.candidate_id, s.observed_at, s.id
                """
            ).fetchall()
            latest_snapshot_ids = {
                int(latest["snapshot_id"])
                for latest in self.connection.execute(
                    "SELECT MAX(id) AS snapshot_id FROM snapshots GROUP BY candidate_id"
                ).fetchall()
            }
            previous_candidate_id: int | None = None
            previous_observation: SiteObservation | None = None
            computed_at = _now()
            for row in rows:
                candidate_id = int(row["candidate_id"])
                if candidate_id != previous_candidate_id:
                    previous_observation = None
                candidate = DiscoveredHost(
                    source_apex=row["source_apex"],
                    hostname=row["hostname"],
                    first_seen=(
                        datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None
                    ),
                    discovery_rank=float(row["discovery_rank"] or 0),
                    label_hint=row["label_hint"] or "",
                    discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
                    provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
                    discovery_assertions=json.loads(row["discovery_assertions_json"] or "[]"),
                )
                observation = SiteObservation.model_validate_json(row["observation_json"])
                score = score_candidate(
                    candidate,
                    observation,
                    previous_observation=previous_observation,
                    ad_intelligence=(
                        AdIntelligence.model_validate(
                            self.current_candidate_ad_intelligence(candidate_id)
                        )
                        if int(row["snapshot_id"]) in latest_snapshot_ids
                        else None
                    ),
                )
                self._upsert_snapshot_target_intelligence_uncommitted(
                    snapshot_id=int(row["snapshot_id"]),
                    candidate_id=candidate_id,
                    candidate=candidate,
                    observation=observation,
                    score=score,
                    computed_at=computed_at,
                )
                previous_candidate_id = candidate_id
                previous_observation = observation

            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, ?)
                """,
                (TARGET_INTELLIGENCE_MIGRATION, computed_at, len(rows)),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _backfill_company_scan_work(self) -> None:
        """Conservatively seed completed website work from auditable historical runs."""
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (COMPANY_SCAN_WORK_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                (COMPANY_SCAN_WORK_MIGRATION,),
            ).fetchone()
            if applied is not None:
                self.connection.commit()
                return

            rows = self.connection.execute(
                """
                WITH latest AS (
                    SELECT candidate_id, MAX(id) AS snapshot_id
                    FROM snapshots
                    GROUP BY candidate_id
                )
                SELECT s.id AS snapshot_id, s.candidate_id, s.run_id,
                       c.hostname, r.status AS run_status, r.finished_at,
                       r.summary_json, ti.canonical_domain, s.observation_json
                FROM snapshots s
                JOIN candidates c ON c.id=s.candidate_id
                JOIN runs r ON r.id=s.run_id
                LEFT JOIN latest l ON l.candidate_id=s.candidate_id
                LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=l.snapshot_id
                WHERE r.status IN ('complete', 'partial')
                  AND r.finished_at IS NOT NULL
                ORDER BY r.finished_at, s.id
                """
            ).fetchall()
            rows_changed = 0
            for row in rows:
                try:
                    observation = SiteObservation.model_validate_json(row["observation_json"])
                except (AttributeError, TypeError, ValueError):
                    continue
                if not observation_completes_company_work(observation):
                    continue
                if row["run_status"] == "partial":
                    try:
                        summary = json.loads(row["summary_json"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    retryable = (
                        summary.get("retryable_hosts") if isinstance(summary, dict) else None
                    )
                    if not isinstance(retryable, list):
                        continue
                    retryable_hosts = {
                        self._normalize_identity_host(str(host)) for host in retryable
                    }
                    if self._normalize_identity_host(row["hostname"]) in retryable_hosts:
                        continue

                company_key = self._normalize_identity_host(row["canonical_domain"])
                if not company_key:
                    company_key = self._normalize_identity_host(row["hostname"])
                if not company_key:
                    continue
                cursor = self.connection.execute(
                    """
                    INSERT INTO company_scan_work(
                        company_key, effort_key, status,
                        completed_candidate_id, completed_run_id,
                        completed_snapshot_id, completed_at, last_error, updated_at
                    ) VALUES (?, ?, 'complete', ?, ?, ?, ?, '', ?)
                    ON CONFLICT(company_key, effort_key) DO UPDATE SET
                        status='complete',
                        claimed_candidate_id=NULL,
                        claimed_run_id=NULL,
                        claimed_at=NULL,
                        lease_expires_at=NULL,
                        completed_candidate_id=excluded.completed_candidate_id,
                        completed_run_id=excluded.completed_run_id,
                        completed_snapshot_id=excluded.completed_snapshot_id,
                        completed_at=excluded.completed_at,
                        last_error='',
                        updated_at=excluded.updated_at
                    WHERE company_scan_work.status<>'running'
                      AND (
                          company_scan_work.completed_at IS NULL
                          OR company_scan_work.completed_at<excluded.completed_at
                      )
                    """,
                    (
                        company_key,
                        COMPANY_SCAN_EFFORT,
                        int(row["candidate_id"]),
                        int(row["run_id"]),
                        int(row["snapshot_id"]),
                        row["finished_at"],
                        row["finished_at"],
                    ),
                )
                rows_changed += max(cursor.rowcount, 0)

            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, ?)
                """,
                (COMPANY_SCAN_WORK_MIGRATION, _now(), rows_changed),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _repair_invalid_company_scan_work(self) -> None:
        """Invalidate legacy completion rows that point at retryable observations."""
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?",
            (COMPANY_SCAN_WORK_REPAIR_MIGRATION,),
        ).fetchone()
        if applied is not None:
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            applied = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?",
                (COMPANY_SCAN_WORK_REPAIR_MIGRATION,),
            ).fetchone()
            if applied is not None:
                self.connection.commit()
                return
            rows = self.connection.execute(
                """
                SELECT work.company_key, work.effort_key
                FROM company_scan_work work
                LEFT JOIN snapshots snapshot ON snapshot.id=work.completed_snapshot_id
                WHERE work.status='complete'
                  AND CASE
                    WHEN snapshot.id IS NULL THEN 1
                    WHEN json_valid(snapshot.observation_json)=0 THEN 1
                    WHEN COALESCE(
                        json_extract(snapshot.observation_json, '$.error'), ''
                    )<>'' THEN 1
                    WHEN COALESCE(
                        json_extract(snapshot.observation_json, '$.fetch_outcome'), 'unknown'
                    )='retryable_error' THEN 1
                    WHEN CAST(COALESCE(
                        json_extract(snapshot.observation_json, '$.status_code'), 0
                    ) AS INTEGER)>=500
                      AND COALESCE(
                        json_extract(snapshot.observation_json, '$.fetch_outcome'), 'unknown'
                      ) NOT IN (
                        'platform_blocked', 'not_public_storefront',
                        'retired_hostname', 'circuit_open'
                      ) THEN 1
                    ELSE 0
                  END=1
                """
            ).fetchall()
            repaired_at = _now()
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE company_scan_work
                    SET status='failed',
                        claimed_candidate_id=NULL, claimed_run_id=NULL,
                        claimed_at=NULL, lease_expires_at=NULL,
                        completed_candidate_id=NULL, completed_run_id=NULL,
                        completed_snapshot_id=NULL, completed_at=NULL,
                        last_error='Invalid historical completion: retryable observation',
                        updated_at=?
                    WHERE company_key=? AND effort_key=? AND status='complete'
                    """,
                    (repaired_at, str(row["company_key"]), str(row["effort_key"])),
                )
            self.connection.execute(
                """
                INSERT INTO schema_migrations(name, applied_at, rows_changed)
                VALUES (?, ?, ?)
                """,
                (COMPANY_SCAN_WORK_REPAIR_MIGRATION, repaired_at, len(rows)),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _target_intelligence_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
        keys = set(row.keys())
        if "target_hypothesis_json" not in keys or not row["target_hypothesis_json"]:
            return None
        hypothesis = CreativeCapacityHypothesis.model_validate_json(row["target_hypothesis_json"])
        principal_signals = hypothesis.principal_signals.model_dump(mode="json")
        criteria_gaps = [
            {
                "key": key,
                "label": key.replace("_", " ").capitalize(),
                "state": value["truth_state"],
                "requires": "additional_public_research",
            }
            for key, value in principal_signals.items()
            if value["truth_state"] != "met"
        ]
        manual_gates = [
            {
                **gate,
                "state": "unknown",
                "requires": "human_verification",
            }
            for gate in TARGET_MANUAL_GATES
        ]
        criteria_gaps.extend(manual_gates)
        unassessable_criteria = [
            {
                **criterion,
                "state": "unknown",
                "requires": "external_or_human_research",
            }
            for criterion in TARGET_PUBLIC_EVIDENCE_LIMITATIONS
        ]
        principal_signals_unknown = [
            key for key, value in principal_signals.items() if value["truth_state"] == "unknown"
        ]
        why_now = hypothesis.decision_packet.why_now or next(
            (item.claim for item in hypothesis.observations if item.dimension == "timing_access"),
            "",
        )
        hypothesis_payload = hypothesis.model_dump(mode="json")
        hypothesis_payload["why_now"] = why_now
        target_score = int(row["target_score"])
        opportunity = hypothesis.opportunity.model_dump(mode="json")
        decision_packet = hypothesis.decision_packet.model_dump(mode="json")
        founder_priority = int(
            row["target_founder_priority_score"]
            if "target_founder_priority_score" in keys
            else opportunity["founder_priority"]
        )
        if not founder_priority:
            founder_priority = int(opportunity["founder_priority"] or target_score)
        next_best_scan = hypothesis.next_best_scan
        leading_gap = (
            {
                "key": next_best_scan,
                "label": next_best_scan.replace("_", " ").capitalize(),
                "state": "unknown",
                "requires": "next_best_scan",
            }
            if next_best_scan != "none"
            else criteria_gaps[0]
            if criteria_gaps
            else None
        )
        return {
            "hydration_status": "hydrated",
            "company_role": hypothesis.company_role,
            "rubric_version": row["target_rubric_version"],
            "canonical_domain": row["target_canonical_domain"],
            "relationship_lane": row["target_relationship_lane"],
            "target_scope": row["target_scope"],
            "archetype": row["target_archetype"],
            "target_score": target_score,
            "priority_score": founder_priority,
            "priority_band": (
                "high"
                if founder_priority >= 70
                else "medium"
                if founder_priority >= 50
                else "developing"
            ),
            "durable_fit_score": opportunity["durable_fit"],
            "contact_now_score": opportunity["contact_now"],
            "founder_priority_score": founder_priority,
            "recommended_action": decision_packet["action"],
            "opportunity": opportunity,
            "decision_packet": decision_packet,
            "signal_events": [event.model_dump(mode="json") for event in hypothesis.signal_events],
            "signal_clusters": [
                cluster.model_dump(mode="json") for cluster in hypothesis.signal_clusters
            ],
            "next_best_scan": next_best_scan,
            "principal_signals_met": int(row["target_principal_signals_met"]),
            "review_ready": bool(row["target_review_ready"]),
            "data_confidence": int(row["target_data_confidence"]),
            "live": bool(row["target_live"]),
            "disqualified": bool(row["target_disqualified"]),
            "computed_at": row["target_computed_at"],
            "dimensions": {
                "distribution_investment": hypothesis.distribution_investment,
                "recurring_creative_demand": hypothesis.recurring_creative_demand,
                "production_capacity_gap": hypothesis.production_capacity_gap,
                "ai_brand_readiness": hypothesis.ai_brand_readiness,
                "timing_access": hypothesis.timing_access,
            },
            "principal_signals": principal_signals,
            "principal_signals_unknown": principal_signals_unknown,
            "derived_signals": hypothesis.derived_signals,
            "observations": [item.model_dump(mode="json") for item in hypothesis.observations],
            "inferences": [item.model_dump(mode="json") for item in hypothesis.inferences],
            "blockers": hypothesis.blockers,
            "criteria_gaps": criteria_gaps,
            "evidence_gaps": criteria_gaps,
            "unassessable_criteria": unassessable_criteria,
            "manual_gates": manual_gates,
            "leading_gap": leading_gap,
            "observed_claim_count": len(hypothesis.observations),
            "inferred_claim_count": len(hypothesis.inferences),
            "why_now": why_now,
            "hypothesis": hypothesis_payload,
            "observed_at": row["observed_at"] if "observed_at" in keys else None,
        }

    @staticmethod
    def _normalized_candidate_ids(candidate_ids: list[int]) -> list[int]:
        return list(
            dict.fromkeys(
                candidate_id
                for raw_candidate_id in candidate_ids
                if (candidate_id := int(raw_candidate_id)) > 0
            )
        )

    @staticmethod
    def _normalized_effort_key(effort_key: str) -> str:
        normalized = effort_key.strip()[:100]
        if not normalized:
            raise ValueError("Company scan effort key cannot be blank")
        return normalized

    def _candidate_company_keys(self, candidate_ids: list[int]) -> dict[int, str]:
        """Resolve current canonical identity, retaining hostname as a safe fallback."""
        identifiers = self._normalized_candidate_ids(candidate_ids)
        company_keys: dict[int, str] = {}
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT c.id AS candidate_id, c.hostname, ti.canonical_domain
                FROM candidates c
                LEFT JOIN snapshots s ON s.id=(
                    SELECT MAX(latest.id)
                    FROM snapshots latest
                    WHERE latest.candidate_id=c.id
                )
                LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                WHERE c.id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                company_key = self._normalize_identity_host(row["canonical_domain"])
                if not company_key:
                    company_key = self._normalize_identity_host(row["hostname"])
                if company_key:
                    company_keys[int(row["candidate_id"])] = company_key
        return company_keys

    def _snapshot_payload(self, snapshot_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            f"""
            SELECT s.observed_at, s.observation_json, s.score_json,
                   s.ai_json, s.final_score,
                   {TARGET_PROJECTION_SELECT}
            FROM snapshots s
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE s.id=?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            target_intelligence = self._target_intelligence_from_row(row)
            score = ScoreCard.model_validate_json(row["score_json"])
            if target_intelligence is not None:
                score.creative_capacity = CreativeCapacityHypothesis.model_validate(
                    target_intelligence["hypothesis"]
                )
            return {
                "observed_at": row["observed_at"],
                "observation": SiteObservation.model_validate_json(row["observation_json"]),
                "score": score,
                "ai": (AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None),
                "final_score": int(row["final_score"]),
                "target_intelligence": target_intelligence,
            }
        except (AttributeError, TypeError, ValueError):
            return None

    def _snapshot_completes_company_work(self, snapshot_id: int) -> bool:
        row = self.connection.execute(
            "SELECT observation_json FROM snapshots WHERE id=?",
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            return False
        try:
            observation = SiteObservation.model_validate_json(row["observation_json"])
        except (AttributeError, TypeError, ValueError):
            return False
        return observation_completes_company_work(observation)

    def _completed_company_snapshots_uncommitted(
        self,
        candidate_ids: list[int],
        *,
        effort_key: str,
        now: datetime,
        max_age_hours: float | None = None,
        recent_observation_hours: float | None = None,
    ) -> dict[int, dict[str, Any]]:
        identifiers = self._normalized_candidate_ids(candidate_ids)
        company_keys = self._candidate_company_keys(identifiers)
        unique_keys = list(dict.fromkeys(company_keys.values()))
        work_by_key: dict[str, sqlite3.Row] = {}
        for start in range(0, len(unique_keys), 500):
            batch = unique_keys[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT company_key, completed_snapshot_id, completed_at
                FROM company_scan_work
                WHERE effort_key=? AND status='complete'
                  AND completed_at IS NOT NULL
                  AND completed_snapshot_id IS NOT NULL
                  AND company_key IN ({placeholders})
                """,
                [effort_key, *batch],
            ).fetchall()
            work_by_key.update({str(row["company_key"]): row for row in rows})

        cutoff = (
            now - timedelta(hours=max(0.0, float(max_age_hours)))
            if max_age_hours is not None
            else None
        )
        observed_cutoff = (
            now - timedelta(hours=recent_observation_hours)
            if recent_observation_hours is not None else None
        )
        payload_by_key: dict[str, dict[str, Any]] = {}
        for company_key, work in work_by_key.items():
            try:
                completed_at = datetime.fromisoformat(str(work["completed_at"]))
            except (TypeError, ValueError):
                continue
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=UTC)
            if completed_at > now:
                continue
            if cutoff is not None and completed_at <= cutoff:
                if observed_cutoff is None:
                    continue
                snapshot = self.connection.execute(
                    "SELECT observed_at FROM snapshots WHERE id=?",
                    (work["completed_snapshot_id"],),
                ).fetchone()
                observed = self._datetime_or_none(snapshot[0]) if snapshot else None
                if observed is None or observed <= observed_cutoff:
                    continue
            payload = self._snapshot_payload(int(work["completed_snapshot_id"]))
            if payload is None:
                continue
            observation = payload.get("observation")
            if not isinstance(observation, SiteObservation) or not (
                observation_completes_company_work(observation)
            ):
                continue
            payload_by_key[company_key] = {
                **payload,
                "completed_at": str(work["completed_at"]),
                "company_key": company_key,
            }
        return {
            candidate_id: payload_by_key[company_key]
            for candidate_id in identifiers
            if (company_key := company_keys.get(candidate_id)) in payload_by_key
        }

    def completed_company_snapshots(
        self,
        candidate_ids: list[int],
        effort_key: str = COMPANY_SCAN_EFFORT,
        *,
        max_age_hours: float | None = None,
        recent_observation_hours: float | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Return latest usable completions, optionally bounded by completion age."""
        return self._completed_company_snapshots_uncommitted(
            candidate_ids,
            effort_key=self._normalized_effort_key(effort_key),
            now=datetime.now(UTC),
            max_age_hours=max_age_hours,
            recent_observation_hours=recent_observation_hours,
        )

    def enrichment_retry_snapshots(self, candidate_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Recent live homepage evidence with only site-owned enrichment left to retry.

        Called after an authoritative company claim; this method never bypasses a lease/retry
        delay. A newer website/identity failure prevents reuse. Raw snapshots stay unchanged.
        """
        result = {}
        keys = self._candidate_company_keys(candidate_ids)
        cutoff = datetime.now(UTC) - timedelta(hours=COMPANY_SCAN_FRESH_HOURS)
        for cid, key in keys.items():
            row = self.connection.execute(
                "SELECT s.* FROM snapshots s JOIN snapshot_target_intelligence ti "
                "ON ti.snapshot_id=s.id WHERE ti.canonical_domain=? "
                "ORDER BY s.observed_at DESC,s.id DESC LIMIT 1", (key,),
            ).fetchone()
            if row is None:
                continue
            observation = SiteObservation.model_validate_json(row["observation_json"])
            if (not observation.live or not observation.final_url or observation.error
                or observation.fetch_outcome not in {"live", "unknown"}
                or not observation.core_hydration_errors
                or not cutoff < self._utc_datetime(observation.observed_at) <= datetime.now(UTC)):
                continue
            result[cid] = {
                "observation": observation.model_copy(update={"core_hydration_reused": True}),
                "score": ScoreCard.model_validate_json(row["score_json"]),
                "ai": AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None,
            }
        return result

    @staticmethod
    def _lease_is_active(value: str | None, *, now: datetime) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > now

    def claim_company_scan_work(
        self,
        candidate_ids: list[int],
        *,
        run_id: int,
        maximum: int = 0,
        force_refresh: bool = False,
        manual_company_refresh: bool = False,
        effort_key: str = COMPANY_SCAN_EFFORT,
    ) -> dict[str, Any]:
        """Atomically reuse, reject, defer, or claim canonical company website work.

        A usable completed company is a hard reuse for 90 days. ``force_refresh`` is
        intentionally narrower: it may retry failed/incomplete work or bypass a longer
        adaptive cadence after that floor, but it cannot override a recent completion.
        ``manual_company_refresh`` is reserved for an explicit company or tag operator action and
        may bypass completion and failure dates, but never another worker's active lease.
        """
        identifiers = self._normalized_candidate_ids(candidate_ids)
        normalized_effort = self._normalized_effort_key(effort_key)
        normalized_maximum = max(0, int(maximum))
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        lease_expires_at = (now + timedelta(minutes=COMPANY_SCAN_LEASE_MINUTES)).isoformat()
        result: dict[str, Any] = {
            "claimed_candidate_ids": [],
            "recent_snapshots": {},
            "retry_deferred_candidate_ids": [],
            "active_candidate_ids": [],
            "deduplicated_candidate_ids": [],
            "deferred_candidate_ids": [],
        }

        with child_step("company_claim_begin_immediate"):
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Company scan claim run does not exist")

            company_keys = self._candidate_company_keys(identifiers)
            representatives: list[tuple[int, str]] = []
            seen_keys: set[str] = set()
            for candidate_id in identifiers:
                company_key = company_keys.get(candidate_id)
                if not company_key:
                    result["deferred_candidate_ids"].append(candidate_id)
                    continue
                if company_key in seen_keys:
                    result["deduplicated_candidate_ids"].append(candidate_id)
                    continue
                seen_keys.add(company_key)
                representatives.append((candidate_id, company_key))

            with child_step("company_claim_snapshot_revalidation"):
                recent_snapshots = (
                    {}
                    if manual_company_refresh
                    else self._completed_company_snapshots_uncommitted(
                        [candidate_id for candidate_id, _ in representatives],
                        effort_key=normalized_effort,
                        now=now,
                        max_age_hours=COMPANY_SCAN_FRESH_HOURS,
                    )
                )
            work_by_key: dict[str, sqlite3.Row] = {}
            company_key_values = [company_key for _, company_key in representatives]
            for start in range(0, len(company_key_values), 500):
                batch = company_key_values[start : start + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = self.connection.execute(
                    f"""
                    SELECT * FROM company_scan_work
                    WHERE effort_key=? AND company_key IN ({placeholders})
                    """,
                    [normalized_effort, *batch],
                ).fetchall()
                work_by_key.update({str(row["company_key"]): row for row in rows})

            for candidate_id, company_key in representatives:
                if candidate_id in recent_snapshots:
                    result["recent_snapshots"][candidate_id] = recent_snapshots[candidate_id]
                    continue
                work = work_by_key.get(company_key)
                if work is not None and work["status"] == "running":
                    same_claim = (
                        int(work["claimed_run_id"] or 0) == int(run_id)
                        and int(work["claimed_candidate_id"] or 0) == candidate_id
                    )
                    if self._lease_is_active(work["lease_expires_at"], now=now):
                        if same_claim:
                            self.connection.execute(
                                """
                                UPDATE company_scan_work
                                SET lease_expires_at=?, updated_at=?
                                WHERE company_key=? AND effort_key=?
                                """,
                                (
                                    lease_expires_at,
                                    timestamp,
                                    company_key,
                                    normalized_effort,
                                ),
                            )
                            result["claimed_candidate_ids"].append(candidate_id)
                        else:
                            result["active_candidate_ids"].append(candidate_id)
                        continue
                if (
                    not manual_company_refresh
                    and work is not None
                    and work["status"] == "failed"
                    and (
                        next_retry_at := self._datetime_or_none(work["next_retry_at"])
                    ) is not None
                    and next_retry_at > now
                ):
                    result["retry_deferred_candidate_ids"].append(candidate_id)
                    continue
                if (
                    normalized_maximum
                    and len(result["claimed_candidate_ids"]) >= normalized_maximum
                ):
                    result["deferred_candidate_ids"].append(candidate_id)
                    continue
                self.connection.execute(
                    """
                    INSERT INTO company_scan_work(
                        company_key, effort_key, status,
                        claimed_candidate_id, claimed_run_id, claimed_at,
                        lease_expires_at, last_error, updated_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, '', ?)
                    ON CONFLICT(company_key, effort_key) DO UPDATE SET
                        status='running',
                        claimed_candidate_id=excluded.claimed_candidate_id,
                        claimed_run_id=excluded.claimed_run_id,
                        claimed_at=excluded.claimed_at,
                        lease_expires_at=excluded.lease_expires_at,
                        next_retry_at=NULL,
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (
                        company_key,
                        normalized_effort,
                        candidate_id,
                        run_id,
                        timestamp,
                        lease_expires_at,
                        timestamp,
                    ),
                )
                result["claimed_candidate_ids"].append(candidate_id)
            with child_step("company_claim_commit"):
                self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def _candidate_run_snapshot_identity(
        self,
        candidate_id: int,
        run_id: int,
    ) -> tuple[int, str] | None:
        row = self.connection.execute(
            """
            SELECT s.id AS snapshot_id, c.hostname, ti.canonical_domain
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE s.candidate_id=? AND s.run_id=?
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (candidate_id, run_id),
        ).fetchone()
        if row is None:
            return None
        company_key = self._normalize_identity_host(row["canonical_domain"])
        if not company_key:
            company_key = self._normalize_identity_host(row["hostname"])
        if not company_key:
            return None
        return int(row["snapshot_id"]), company_key

    def _upsert_company_completion_uncommitted(
        self,
        *,
        company_key: str,
        effort_key: str,
        candidate_id: int,
        run_id: int,
        snapshot_id: int,
        completed_at: str,
        updated_at: str,
    ) -> bool:
        if not self._snapshot_completes_company_work(snapshot_id):
            return False
        existing = self.connection.execute(
            """
            SELECT status, claimed_run_id, lease_expires_at
            FROM company_scan_work
            WHERE company_key=? AND effort_key=?
            """,
            (company_key, effort_key),
        ).fetchone()
        if (
            existing is not None
            and existing["status"] == "running"
            and int(existing["claimed_run_id"] or 0) != run_id
            and self._lease_is_active(existing["lease_expires_at"], now=datetime.now(UTC))
        ):
            return False
        self.connection.execute(
            """
            INSERT INTO company_scan_work(
                company_key, effort_key, status,
                completed_candidate_id, completed_run_id,
                completed_snapshot_id, completed_at, last_error, updated_at
            ) VALUES (?, ?, 'complete', ?, ?, ?, ?, '', ?)
            ON CONFLICT(company_key, effort_key) DO UPDATE SET
                status='complete',
                claimed_candidate_id=NULL,
                claimed_run_id=NULL,
                claimed_at=NULL,
                lease_expires_at=NULL,
                completed_candidate_id=excluded.completed_candidate_id,
                completed_run_id=excluded.completed_run_id,
                completed_snapshot_id=excluded.completed_snapshot_id,
                completed_at=excluded.completed_at,
                next_retry_at=NULL,
                last_error='',
                updated_at=excluded.updated_at
            """,
            (
                company_key,
                effort_key,
                candidate_id,
                run_id,
                snapshot_id,
                completed_at,
                updated_at,
            ),
        )
        return True

    def _complete_company_claim_uncommitted(
        self,
        candidate_id: int,
        run_id: int,
        *,
        effort_key: str,
        completed_at: str,
    ) -> bool:
        identity = self._candidate_run_snapshot_identity(candidate_id, run_id)
        if identity is None:
            return False
        snapshot_id, canonical_key = identity
        if not self._snapshot_completes_company_work(snapshot_id):
            return False
        claim_rows = self.connection.execute(
            """
            SELECT company_key
            FROM company_scan_work
            WHERE effort_key=? AND status='running'
              AND claimed_run_id=? AND claimed_candidate_id=?
            """,
            (effort_key, run_id, candidate_id),
        ).fetchall()
        if not claim_rows:
            already_complete = self.connection.execute(
                """
                SELECT completed_at
                FROM company_scan_work
                WHERE effort_key=? AND status='complete'
                  AND completed_run_id=? AND completed_candidate_id=?
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (effort_key, run_id, candidate_id),
            ).fetchone()
            if already_complete is None:
                return False
            completed_at = str(already_complete["completed_at"] or completed_at)

        claimed_keys = {str(row["company_key"]) for row in claim_rows}
        completed = False
        for company_key in claimed_keys:
            completed = self._upsert_company_completion_uncommitted(
                company_key=company_key,
                effort_key=effort_key,
                candidate_id=candidate_id,
                run_id=run_id,
                snapshot_id=snapshot_id,
                completed_at=completed_at,
                updated_at=completed_at,
            ) or completed
        if canonical_key not in claimed_keys:
            completed = self._upsert_company_completion_uncommitted(
                company_key=canonical_key,
                effort_key=effort_key,
                candidate_id=candidate_id,
                run_id=run_id,
                snapshot_id=snapshot_id,
                completed_at=completed_at,
                updated_at=completed_at,
            ) or completed
        return completed

    def finish_run_with_company_scan_work(
        self,
        run_id: int,
        status: str,
        summary: dict[str, Any],
        *,
        completed_candidate_ids: list[int],
        failed_candidate_ids: list[int],
        failure_reason: str = "",
    ) -> None:
        """Finish one run and settle every company claim in the same transaction."""
        completed_ids = self._normalized_candidate_ids(completed_candidate_ids)
        failed_ids = self._normalized_candidate_ids(failed_candidate_ids)
        overlap = sorted(set(completed_ids) & set(failed_ids))
        if overlap:
            raise ValueError("Completed and failed company scan candidates cannot overlap")
        timestamp = _now()
        error = failure_reason.strip()[:2_000]
        next_retry_at = (
            datetime.fromisoformat(timestamp)
            + timedelta(hours=COMPANY_SCAN_FAILURE_RETRY_HOURS)
        ).isoformat()

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Company scan run does not exist")
            timings = self._track_run_phase(run_id, status)
            if timings is not None:
                summary = {**summary, "phase_wall_seconds": timings}
            self.connection.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
                (timestamp, status, json.dumps(summary, sort_keys=True), run_id),
            )
            self.connection.execute(
                "UPDATE run_progress SET phase=?, updated_at=? WHERE run_id=?",
                (status, timestamp, run_id),
            )
            for candidate_id in completed_ids:
                self._complete_company_claim_uncommitted(
                    candidate_id,
                    run_id,
                    effort_key=COMPANY_SCAN_EFFORT,
                    completed_at=timestamp,
                )
            for start in range(0, len(failed_ids), 500):
                batch = failed_ids[start : start + 500]
                placeholders = ", ".join("?" for _ in batch)
                self.connection.execute(
                    f"""
                    UPDATE company_scan_work
                    SET status='failed', claimed_candidate_id=NULL,
                        claimed_run_id=NULL, claimed_at=NULL,
                        lease_expires_at=NULL, next_retry_at=?, last_error=?, updated_at=?
                    WHERE effort_key=? AND status='running' AND claimed_run_id=?
                      AND claimed_candidate_id IN ({placeholders})
                    """,
                    [next_retry_at, error, timestamp, COMPANY_SCAN_EFFORT, run_id, *batch],
                )
            self.connection.execute(
                """
                UPDATE company_scan_work
                SET status='failed', claimed_candidate_id=NULL,
                    claimed_run_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, next_retry_at=?,
                    last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END,
                    updated_at=?
                WHERE effort_key=? AND status='running' AND claimed_run_id=?
                """,
                (
                    next_retry_at,
                    error,
                    error,
                    timestamp,
                    COMPANY_SCAN_EFFORT,
                    run_id,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def defer_company_scan_work(self, run_id: int, candidate_id: int, hostname: str) -> None:
        """Release local capacity gaps without publishing a failed website or retry clock."""
        timestamp = _now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE company_scan_work
                SET status='failed', claimed_candidate_id=NULL, claimed_run_id=NULL,
                    claimed_at=NULL, lease_expires_at=NULL, next_retry_at=NULL,
                    last_error='', updated_at=?
                WHERE status='running' AND claimed_run_id=? AND claimed_candidate_id=?
                """,
                (timestamp, run_id, candidate_id),
            )
            self.connection.execute(
                "INSERT INTO source_errors(run_id,source_apex,error,created_at) VALUES(?,?,?,?)",
                (run_id, hostname, "Website check deferred by local request capacity", timestamp),
            )

    def release_company_scan_claims(self, run_id: int, reason: str = "") -> int:
        """Release every live company claim owned by one abandoned run."""
        timestamp = _now()
        error = reason.strip()[:2_000]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE company_scan_work
                SET status='failed', claimed_candidate_id=NULL,
                    claimed_run_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, last_error=?, updated_at=?
                WHERE status='running' AND claimed_run_id=?
                """,
                (error, timestamp, run_id),
            )
            self.connection.commit()
            return cursor.rowcount
        except Exception:
            self.connection.rollback()
            raise

    def update_completed_company_snapshot(
        self,
        candidate_id: int,
        run_id: int,
        effort_key: str = COMPANY_SCAN_EFFORT,
    ) -> bool:
        """Point reused completion at a newer local snapshot without resetting freshness."""
        normalized_effort = self._normalized_effort_key(effort_key)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            identity = self._candidate_run_snapshot_identity(candidate_id, run_id)
            if identity is None:
                self.connection.commit()
                return False
            snapshot_id, company_key = identity
            if not self._snapshot_completes_company_work(snapshot_id):
                self.connection.commit()
                return False
            rows = self.connection.execute(
                """
                SELECT company_key, completed_candidate_id, completed_run_id, completed_at
                FROM company_scan_work
                WHERE effort_key=? AND status='complete'
                  AND (company_key=? OR completed_candidate_id=?)
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                """,
                (normalized_effort, company_key, candidate_id),
            ).fetchall()
            if not rows:
                self.connection.commit()
                return False
            completed_at = str(rows[0]["completed_at"])
            updated_at = _now()
            updated = False
            for row in rows:
                cursor = self.connection.execute(
                    """
                    UPDATE company_scan_work
                    SET completed_snapshot_id=?, updated_at=?
                    WHERE company_key=? AND effort_key=? AND status='complete'
                    """,
                    (
                        snapshot_id,
                        updated_at,
                        str(row["company_key"]),
                        normalized_effort,
                    ),
                )
                updated = bool(cursor.rowcount) or updated
            if company_key not in {str(row["company_key"]) for row in rows}:
                updated = (
                    self._upsert_company_completion_uncommitted(
                        company_key=company_key,
                        effort_key=normalized_effort,
                        candidate_id=int(rows[0]["completed_candidate_id"] or candidate_id),
                        run_id=int(rows[0]["completed_run_id"] or run_id),
                        snapshot_id=snapshot_id,
                        completed_at=completed_at,
                        updated_at=updated_at,
                    )
                    or updated
                )
            self.connection.commit()
            return updated
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _normalized_scanner_identity(
        scanner_id: str,
        scanner_version: str,
    ) -> tuple[str, str]:
        normalized_id = scanner_id.strip()[:120]
        normalized_version = scanner_version.strip()[:120]
        if not normalized_id or not normalized_version:
            raise ValueError("Scanner id and version cannot be blank")
        return normalized_id, normalized_version

    @classmethod
    def _normalized_company_key(cls, company_key: str) -> str:
        return cls._normalize_identity_host(company_key) or company_key.strip().lower()[:253]

    @staticmethod
    def _datetime_or_none(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    _utc_datetime = staticmethod(naive_as_utc)

    @classmethod
    def _frontier_timestamp(cls, value: datetime) -> str:
        return cls._utc_datetime(value).isoformat()

    @classmethod
    def _bounded_frontier_scan_timestamp(
        cls,
        value: datetime,
        *,
        recorded_at: str,
    ) -> str:
        """Keep worker scan clocks from advancing beyond the durable ledger clock.

        These fields describe when our scanner fetched or observed something. They are
        distinct from source-owned dates such as ``published_at`` or ``effective_at``,
        which may legitimately be in the future (for example, an upcoming event).
        Clamping instead of rejecting tolerates harmless worker clock skew while making
        append order authoritative for later tombstones.
        """
        durable_at = cls._datetime_or_none(recorded_at)
        if durable_at is None:  # pragma: no cover - recorded_at is generated internally
            raise ValueError("Durable frontier timestamp is invalid")
        return min(cls._utc_datetime(value), durable_at).isoformat()

    def claim_scanner_work(
        self,
        candidate_ids: list[int],
        *,
        scanner_id: str,
        scanner_version: str,
        run_id: int | None = None,
        extractor_version: str = "",
        scope_key: str = "",
        config: dict[str, Any] | None = None,
        maximum: int = 0,
        force_refresh: bool = False,
        lease_minutes: int = SCANNER_WORK_LEASE_MINUTES,
        independent_scope: bool = False,
        manual_contact_refresh: bool = False,
    ) -> dict[str, Any]:
        """Atomically claim one optional scanner per canonical company.

        A successful, empty-success, or fully covered not-applicable result is normally
        a hard reuse for the next 90 days, including completed empty people searches.
        ``force_refresh`` may bypass
        other longer scanner-specific ``next_due_at`` windows, but never the applicable
        hard reuse window. The dedicated contact crawl can explicitly bypass a
        completed scope's hold while preserving retries and active leases.
        Explicit independent scopes isolate request receipts, leases and reuse only for
        the Microlink hydration owner; existing scanner method-level policies are unchanged.
        """
        identifiers = self._normalized_candidate_ids(candidate_ids)
        normalized_id, normalized_version = self._normalized_scanner_identity(
            scanner_id,
            scanner_version,
        )
        if independent_scope and (normalized_id != "microlink_hydration_v1" or not scope_key):
            raise ValueError("Independent scopes require a named Microlink request")
        if manual_contact_refresh and (
            not independent_scope or (config or {}).get("contact_hydration") is not True
        ):
            raise ValueError("Manual contact refresh requires a dedicated contact request scope")
        normalized_maximum = max(0, int(maximum))
        normalized_lease = max(1, min(int(lease_minutes), 24 * 60))
        config_json = json.dumps(config or {}, sort_keys=True, separators=(",", ":"))
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        freshness_cutoff = now - REHYDRATION_INTERVAL
        lease_expires_at = (now + timedelta(minutes=normalized_lease)).isoformat()
        result: dict[str, Any] = {
            "claimed": {},
            "claimed_candidate_ids": [],
            "reused_executions": {},
            "reused_candidate_ids": [],
            "not_due_candidate_ids": [],
            "active_candidate_ids": [],
            "deduplicated_candidate_ids": [],
            "deferred_candidate_ids": [],
            "alias_of": {},
        }

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if run_id is not None:
                run = self.connection.execute(
                    "SELECT id FROM runs WHERE id=?",
                    (int(run_id),),
                ).fetchone()
                if run is None:
                    raise ValueError("Scanner claim run does not exist")

            company_keys = self._candidate_company_keys(identifiers)
            representatives: list[tuple[int, str]] = []
            representative_by_key: dict[str, int] = {}
            for candidate_id in identifiers:
                company_key = company_keys.get(candidate_id)
                if not company_key:
                    result["deferred_candidate_ids"].append(candidate_id)
                    continue
                if company_key in representative_by_key:
                    result["deduplicated_candidate_ids"].append(candidate_id)
                    result["alias_of"][candidate_id] = representative_by_key[company_key]
                    continue
                representative_by_key[company_key] = candidate_id
                representatives.append((candidate_id, company_key))

            work_by_key: dict[str, sqlite3.Row] = {}
            method_work_by_key: dict[str, list[sqlite3.Row]] = {}
            company_key_values = [company_key for _, company_key in representatives]
            for start in range(0, len(company_key_values), 500):
                batch = company_key_values[start : start + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = self.connection.execute(
                    f"""
                    SELECT sw.*,se.status AS completed_status
                    FROM scanner_work sw
                    LEFT JOIN scanner_executions se
                      ON se.id=sw.completed_execution_id
                    WHERE sw.scanner_id=? AND sw.company_key IN ({placeholders})
                      AND (?=0 OR EXISTS (
                        SELECT 1 FROM scanner_executions scoped
                        WHERE scoped.id=COALESCE(sw.claimed_execution_id,sw.completed_execution_id)
                          AND scoped.scope_key=?
                      ))
                    """,
                    [normalized_id, *batch, int(independent_scope), scope_key],
                ).fetchall()
                for row in rows:
                    company_key = str(row["company_key"])
                    method_work_by_key.setdefault(company_key, []).append(row)
                    if str(row["scanner_version"]) == normalized_version:
                        work_by_key[company_key] = row

            for candidate_id, company_key in representatives:
                work = work_by_key.get(company_key)
                method_work = method_work_by_key.get(company_key, [])
                if any(
                    row["status"] == "running"
                    and self._lease_is_active(row["lease_expires_at"], now=now)
                    for row in method_work
                ):
                    result["active_candidate_ids"].append(candidate_id)
                    continue
                retry_at = now + timedelta(hours=SCANNER_FAILURE_RETRY_HOURS)
                for expired_work in (
                    row
                    for row in method_work
                    if row["status"] == "running"
                    and not self._lease_is_active(row["lease_expires_at"], now=now)
                ):
                    expired_execution_id = expired_work["claimed_execution_id"]
                    if expired_execution_id is not None:
                        self.connection.execute(
                            """
                            UPDATE scanner_executions
                            SET status='transient_error', finished_at=?, next_due_at=?,
                                error='Lease expired before completion'
                            WHERE id=? AND status='running'
                            """,
                            (
                                timestamp,
                                retry_at.isoformat(),
                                int(expired_execution_id),
                            ),
                        )
                    self.connection.execute(
                        """
                        UPDATE scanner_work
                        SET status='transient_error', claimed_candidate_id=NULL,
                            claimed_execution_id=NULL, claimed_at=NULL,
                            lease_expires_at=NULL, next_due_at=?,
                            last_error='Lease expired before completion', updated_at=?
                        WHERE company_key=? AND scanner_id=? AND scanner_version=?
                          AND status='running'
                        """,
                        (
                            retry_at.isoformat(),
                            timestamp,
                            company_key,
                            normalized_id,
                            str(expired_work["scanner_version"]),
                        ),
                    )
                    if work is not None and str(work["scanner_version"]) == str(
                        expired_work["scanner_version"]
                    ):
                        work = None
                fresh_reusable = [
                    (completed_at, row)
                    for row in method_work
                    if not manual_contact_refresh
                    and (row["completed_status"] or row["status"]) in SCANNER_REUSABLE_STATUSES
                    and (completed_at := self._datetime_or_none(row["completed_at"])) is not None
                    and freshness_cutoff < completed_at <= now
                ]
                if fresh_reusable:
                    _, reusable_work = max(fresh_reusable, key=lambda item: item[0])
                    result["reused_candidate_ids"].append(candidate_id)
                    if reusable_work["completed_execution_id"] is not None:
                        result["reused_executions"][candidate_id] = int(
                            reusable_work["completed_execution_id"]
                        )
                    continue
                protected_failure = next(
                    (
                        row
                        for row in method_work
                        if row["status"] in {"robots_blocked", "rate_limited"}
                        and (
                            retry_at := self._datetime_or_none(row["next_due_at"])
                        ) is not None
                        and retry_at > now
                    ),
                    None,
                )
                if protected_failure is not None:
                    # Manual force-refresh cannot override an origin's robots policy or an
                    # explicit server throttle, including across scanner-version changes.
                    result["not_due_candidate_ids"].append(candidate_id)
                    continue
                protected_plan = next(
                    (
                        row
                        for row in method_work
                        if row["status"] in SCANNER_TERMINAL_STATUSES
                        and (
                            row["status"] in SCANNER_FAILURE_STATUSES
                            or not manual_contact_refresh
                        )
                        and (
                            planned_at := self._datetime_or_none(row["next_due_at"])
                        ) is not None
                        and planned_at > now
                    ),
                    None,
                )
                if not force_refresh and protected_plan is not None:
                    # Automated scheduling is method-level. A version change must not
                    # bypass a durable success TTL or failure retry plan.
                    result["not_due_candidate_ids"].append(candidate_id)
                    continue
                if (
                    work is not None
                    and work["status"] in SCANNER_REUSABLE_STATUSES
                    and not manual_contact_refresh
                ):
                    next_due_at = self._datetime_or_none(work["next_due_at"])
                    if not force_refresh and next_due_at is not None and next_due_at > now:
                        result["not_due_candidate_ids"].append(candidate_id)
                        continue

                if work is not None and work["status"] in SCANNER_FAILURE_STATUSES:
                    retry_at = self._datetime_or_none(work["next_due_at"])
                    if not force_refresh and retry_at is not None and retry_at > now:
                        result["not_due_candidate_ids"].append(candidate_id)
                        continue

                if work is not None and work["status"] == "running":
                    if self._lease_is_active(work["lease_expires_at"], now=now):
                        # A candidate id is not a worker identity. Returning the live
                        # execution here would let a second process duplicate network
                        # work. Owners renew only by presenting the execution id.
                        result["active_candidate_ids"].append(candidate_id)
                        continue
                    if work["claimed_execution_id"] is not None:
                        self.connection.execute(
                            """
                            UPDATE scanner_executions
                            SET status='transient_error', finished_at=?,
                                error='Lease expired before completion'
                            WHERE id=? AND status='running'
                            """,
                            (timestamp, int(work["claimed_execution_id"])),
                        )

                if (
                    normalized_maximum
                    and len(result["claimed_candidate_ids"]) >= normalized_maximum
                ):
                    result["deferred_candidate_ids"].append(candidate_id)
                    continue

                cursor = self.connection.execute(
                    """
                    INSERT INTO scanner_executions(
                        candidate_id, run_id, company_key, scanner_id,
                        scanner_version, extractor_version, scope_key, config_hash,
                        status, started_at, summary_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, '{}', ?)
                    """,
                    (
                        candidate_id,
                        run_id,
                        company_key,
                        normalized_id,
                        normalized_version,
                        extractor_version.strip()[:120],
                        scope_key.strip()[:500],
                        config_hash,
                        timestamp,
                        timestamp,
                    ),
                )
                execution_id = int(cursor.lastrowid)
                self.connection.execute(
                    """
                    INSERT INTO scanner_work(
                        company_key, scanner_id, scanner_version, status,
                        claimed_candidate_id, claimed_execution_id, claimed_at,
                        lease_expires_at, last_error, updated_at
                    ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, '', ?)
                    ON CONFLICT(company_key, scanner_id, scanner_version) DO UPDATE SET
                        status='running',
                        claimed_candidate_id=excluded.claimed_candidate_id,
                        claimed_execution_id=excluded.claimed_execution_id,
                        claimed_at=excluded.claimed_at,
                        lease_expires_at=excluded.lease_expires_at,
                        next_due_at=NULL,
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (
                        company_key,
                        normalized_id,
                        normalized_version,
                        candidate_id,
                        execution_id,
                        timestamp,
                        lease_expires_at,
                        timestamp,
                    ),
                )
                result["claimed"][candidate_id] = execution_id
                result["claimed_candidate_ids"].append(candidate_id)
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def renew_scanner_work_lease(
        self,
        execution_id: int,
        *,
        lease_minutes: int = SCANNER_WORK_LEASE_MINUTES,
    ) -> bool:
        """Renew exactly the live lease owned by one scanner execution."""
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        lease_expires_at = (
            now + timedelta(minutes=max(1, min(int(lease_minutes), 24 * 60)))
        ).isoformat()
        cursor = self.connection.execute(
            """
            UPDATE scanner_work
            SET lease_expires_at=?, updated_at=?
            WHERE status='running' AND claimed_execution_id=?
              AND EXISTS(
                  SELECT 1 FROM scanner_executions se
                  WHERE se.id=? AND se.status='running'
              )
            """,
            (lease_expires_at, timestamp, int(execution_id), int(execution_id)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _assert_scanner_payload_identity(
        row: sqlite3.Row,
        *,
        scanner_id: str,
        scanner_version: str,
        company_key: str | None = None,
    ) -> None:
        if scanner_id != row["scanner_id"] or scanner_version != row["scanner_version"]:
            raise ValueError("Scanner evidence identity does not match its execution")
        if company_key is not None and company_key != row["company_key"]:
            raise ValueError("Scanner evidence company does not match its execution")

    def _persist_scanner_evidence_uncommitted(
        self,
        execution: sqlite3.Row,
        *,
        coverage_manifests: list[ScannerCoverageManifest] | tuple[ScannerCoverageManifest, ...],
        artifacts: list[EvidenceArtifact] | tuple[EvidenceArtifact, ...],
        observations: list[AtomicObservation] | tuple[AtomicObservation, ...],
        lineage: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        recorded_at: str,
    ) -> tuple[int, int, int]:
        require_allowed(execution["company_key"])
        execution_id = int(execution["id"])
        manifest_keys = {item.manifest_key for item in coverage_manifests}
        artifact_by_key = {item.artifact_key: item for item in artifacts}

        def evidence_url(value: str) -> tuple[str, str, str, str]:
            try:
                parsed = urlsplit(value.strip())
                host = (parsed.hostname or "").lower().rstrip(".")
                port = parsed.port
            except ValueError:
                return "", "", "", ""
            netloc = f"{host}:{port}" if port else host
            return parsed.scheme.lower(), netloc, parsed.path.rstrip("/") or "/", parsed.query

        for observation in observations:
            cited_artifacts = set(observation.artifact_keys)
            cited_coverage = set(observation.coverage_keys)
            if not cited_artifacts <= set(artifact_by_key):
                raise ValueError(
                    "Observation artifact citations must belong to the same scanner execution"
                )
            if not cited_coverage <= manifest_keys:
                raise ValueError(
                    "Observation coverage citations must belong to the same scanner execution"
                )
            if observation.score_eligible:
                source_identity = evidence_url(observation.source_url)
                if not cited_coverage:
                    raise ValueError(
                        "Score-eligible observation requires coverage from its execution"
                    )
                exact_artifacts = [
                    artifact_by_key[key]
                    for key in cited_artifacts
                    if key in artifact_by_key
                    and source_identity[0]
                    and source_identity[1]
                    and bool(artifact_by_key[key].content_hash)
                    and evidence_url(artifact_by_key[key].canonical_url)
                    == source_identity
                    and (
                        not observation.extractor_version
                        or artifact_by_key[key].extractor_version
                        == observation.extractor_version
                    )
                ]
                if not exact_artifacts:
                    raise ValueError(
                        "Score-eligible observation requires an exact artifact from its execution"
                    )
        for manifest in coverage_manifests:
            manifest_payload = manifest.model_dump(mode="json")
            self._assert_scanner_payload_identity(
                execution,
                scanner_id=manifest.scanner_id,
                scanner_version=manifest.scanner_version,
            )
            self.connection.execute(
                """
                INSERT INTO coverage_manifests(
                    execution_id, manifest_key, company_key, scanner_id,
                    scanner_version, surface, scope, window_start, window_end,
                    attempted_units, successful_units, failed_units, total_units,
                    completeness, truncated, absence_interpretable,
                    limitations_json, source_urls_json, observed_at, metadata_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    manifest.manifest_key,
                    execution["company_key"],
                    manifest.scanner_id,
                    manifest.scanner_version,
                    manifest.surface,
                    manifest.scope,
                    self._frontier_timestamp(manifest.window_start)
                    if manifest.window_start
                    else None,
                    self._frontier_timestamp(manifest.window_end) if manifest.window_end else None,
                    manifest.attempted_units,
                    manifest.successful_units,
                    manifest.failed_units,
                    manifest.total_units,
                    manifest.completeness,
                    int(manifest.truncated),
                    int(manifest.absence_interpretable),
                    json.dumps(manifest_payload["limitations"], sort_keys=True),
                    json.dumps(manifest_payload["source_urls"], sort_keys=True),
                    self._bounded_frontier_scan_timestamp(
                        manifest.observed_at,
                        recorded_at=recorded_at,
                    ),
                    json.dumps(manifest_payload["metadata"], sort_keys=True),
                    recorded_at,
                ),
            )

        for artifact in artifacts:
            artifact_payload = artifact.model_dump(mode="json")
            self._assert_scanner_payload_identity(
                execution,
                scanner_id=artifact.scanner_id,
                scanner_version=artifact.scanner_version,
                company_key=artifact.company_key,
            )
            self.connection.execute(
                """
                INSERT INTO evidence_artifacts(
                    execution_id, artifact_key, company_key, scanner_id,
                    scanner_version, extractor_version, artifact_type,
                    canonical_url, original_url, publisher, fetched_at,
                    status_code, mime_type, content_hash, published_at,
                    published_time_basis, etag, last_modified, byte_count,
                    robots_state, truncated, excerpt, locator, retention_policy,
                    license_policy, metadata_json, recorded_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    execution_id,
                    artifact.artifact_key,
                    artifact.company_key,
                    artifact.scanner_id,
                    artifact.scanner_version,
                    artifact.extractor_version,
                    artifact.artifact_type,
                    artifact.canonical_url,
                    artifact.original_url,
                    artifact.publisher,
                    self._bounded_frontier_scan_timestamp(
                        artifact.fetched_at,
                        recorded_at=recorded_at,
                    ),
                    artifact.status_code,
                    artifact.mime_type,
                    artifact.content_hash,
                    self._frontier_timestamp(artifact.published_at)
                    if artifact.published_at
                    else None,
                    artifact.published_time_basis,
                    artifact.etag,
                    artifact.last_modified,
                    artifact.byte_count,
                    artifact.robots_state,
                    int(artifact.truncated),
                    artifact.excerpt,
                    artifact.locator,
                    artifact.retention_policy,
                    artifact.license_policy,
                    json.dumps(artifact_payload["metadata"], sort_keys=True),
                    recorded_at,
                ),
            )

        for observation in observations:
            observation_payload = observation.model_dump(mode="json")
            self._assert_scanner_payload_identity(
                execution,
                scanner_id=observation.scanner_id,
                scanner_version=observation.scanner_version,
                company_key=observation.company_key,
            )
            self.connection.execute(
                """
                INSERT INTO atomic_observations(
                    execution_id, observation_key, company_key, scanner_id,
                    scanner_version, extractor_version, rule_id, rule_version,
                    entity_key, predicate, value_type, value_json, unit,
                    qualifiers_json, summary, source_url, epistemic_status,
                    validity_status, confidence, confidence_method,
                    first_observed_at, last_observed_at, effective_at,
                    effective_time_basis, expires_at, artifact_keys_json,
                    coverage_keys_json, score_eligible, discovery_only,
                    source_actor_key, origin_fingerprint, causal_root_key,
                    metadata_json, recorded_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    execution_id,
                    observation.observation_key,
                    observation.company_key,
                    observation.scanner_id,
                    observation.scanner_version,
                    observation.extractor_version,
                    observation.rule_id,
                    observation.rule_version,
                    observation.entity_key,
                    observation.predicate,
                    observation.value_type,
                    json.dumps(observation_payload["value"], sort_keys=True),
                    observation.unit,
                    json.dumps(observation_payload["qualifiers"], sort_keys=True),
                    observation.summary,
                    observation.source_url,
                    observation.epistemic_status,
                    observation.validity_status,
                    observation.confidence,
                    observation.confidence_method,
                    self._bounded_frontier_scan_timestamp(
                        observation.first_observed_at,
                        recorded_at=recorded_at,
                    ),
                    self._bounded_frontier_scan_timestamp(
                        observation.last_observed_at,
                        recorded_at=recorded_at,
                    ),
                    self._frontier_timestamp(observation.effective_at)
                    if observation.effective_at
                    else None,
                    observation.effective_time_basis,
                    self._frontier_timestamp(observation.expires_at)
                    if observation.expires_at
                    else None,
                    json.dumps(observation_payload["artifact_keys"], sort_keys=True),
                    json.dumps(observation_payload["coverage_keys"], sort_keys=True),
                    int(observation.score_eligible),
                    int(observation.discovery_only),
                    observation.source_actor_key,
                    observation.origin_fingerprint,
                    observation.causal_root_key,
                    json.dumps(observation_payload["metadata"], sort_keys=True),
                    recorded_at,
                ),
            )

        allowed_lineage = {
            "extracted_from",
            "corroborates",
            "contradicts",
            "syndicated_from",
            "derived_from",
        }
        observation_keys = {item.observation_key for item in observations}
        artifact_keys = set(artifact_by_key)
        for edge in lineage:
            relationship = str(edge.get("relationship_type", "")).strip()
            subject_key = str(edge.get("subject_observation_key", "")).strip()
            object_key = str(edge.get("object_observation_key", "")).strip() or None
            artifact_key = str(edge.get("artifact_key", "")).strip() or None
            if relationship not in allowed_lineage:
                raise ValueError(f"Unsupported observation lineage: {relationship}")
            if not subject_key or (object_key is None and artifact_key is None):
                raise ValueError("Observation lineage requires a subject and object")
            if subject_key not in observation_keys:
                existing_subject = self.connection.execute(
                    """
                    SELECT 1 FROM atomic_observations
                    WHERE execution_id=? AND observation_key=?
                    """,
                    (execution_id, subject_key),
                ).fetchone()
                if existing_subject is None:
                    raise ValueError("Observation lineage subject is not persisted")
            if object_key is not None and object_key not in observation_keys:
                existing_object = self.connection.execute(
                    """
                    SELECT 1 FROM atomic_observations
                    WHERE company_key=? AND observation_key=?
                    """,
                    (execution["company_key"], object_key),
                ).fetchone()
                if existing_object is None:
                    raise ValueError("Observation lineage object is not persisted")
            if artifact_key is not None and artifact_key not in artifact_keys:
                existing_artifact = self.connection.execute(
                    """
                    SELECT 1 FROM evidence_artifacts
                    WHERE execution_id=? AND artifact_key=?
                    """,
                    (execution_id, artifact_key),
                ).fetchone()
                if existing_artifact is None:
                    raise ValueError("Observation lineage artifact is not persisted")
            self.connection.execute(
                """
                INSERT INTO observation_lineage(
                    execution_id, subject_observation_key, relationship_type,
                    object_observation_key, artifact_key, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    subject_key,
                    relationship,
                    object_key,
                    artifact_key,
                    json.dumps(edge.get("details", {}), sort_keys=True),
                    recorded_at,
                ),
            )
        return len(coverage_manifests), len(artifacts), len(observations)

    def record_market_projection(
        self,
        candidate_id: int,
        company_key: str,
        source_id: str,
        projection: Any,
        *,
        source_run_id: int | None = None,
    ) -> dict[str, Any]:
        """Append resolved market evidence without claiming company-scanner work.

        Market feeds are global source polls, not company-detail scans. New documents can
        legitimately arrive more than once inside 24 hours, so this transaction intentionally
        writes no ``scanner_work`` row and never changes ``company_scan_work``. A content-derived
        hash makes an exact replay a no-op while genuine revisions append immutable evidence.
        """

        normalized_candidate_id = int(candidate_id)
        normalized_company = self._normalized_company_key(company_key)
        expected_company = self._candidate_company_keys([normalized_candidate_id]).get(
            normalized_candidate_id
        )
        if expected_company is None:
            raise ValueError("Market projection candidate does not exist")
        if expected_company != normalized_company:
            raise ValueError("Market projection candidate identity does not match")
        normalized_source = source_id.strip().lower()[:128]
        if not normalized_source:
            raise ValueError("Market projection source id cannot be blank")

        coverage_manifests = tuple(getattr(projection, "coverage_manifests", ()))
        artifacts = tuple(getattr(projection, "artifacts", ()))
        observations = tuple(getattr(projection, "observations", ()))
        if not coverage_manifests or not artifacts:
            raise ValueError("Market projection requires coverage and at least one artifact")
        scanner_ids = {
            str(item.scanner_id)
            for item in (*coverage_manifests, *artifacts, *observations)
        }
        scanner_versions = {
            str(item.scanner_version)
            for item in (*coverage_manifests, *artifacts, *observations)
        }
        if len(scanner_ids) != 1 or len(scanner_versions) != 1:
            raise ValueError("Market projection evidence must share one scanner identity")
        scanner_id = next(iter(scanner_ids))
        scanner_version = next(iter(scanner_versions))
        for artifact in artifacts:
            if self._normalized_company_key(artifact.company_key) != normalized_company:
                raise ValueError("Market projection artifact company does not match")
        for observation in observations:
            if self._normalized_company_key(observation.company_key) != normalized_company:
                raise ValueError("Market projection observation company does not match")
        if any(
            manifest.scope
            and self._normalized_company_key(manifest.scope) != normalized_company
            for manifest in coverage_manifests
        ):
            raise ValueError("Market projection coverage scope does not match")

        projection_identity = {
            "source_id": normalized_source,
            "company_key": normalized_company,
            "scanner_id": scanner_id,
            "scanner_version": scanner_version,
            "artifact_keys": sorted(item.artifact_key for item in artifacts),
            "observation_keys": sorted(item.observation_key for item in observations),
        }
        config_hash = hashlib.sha256(
            json.dumps(
                projection_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = datetime.now(UTC)
        recorded_at = now.isoformat()
        scope_key = f"market_source:{normalized_source}"[:500]
        partial = any(
            manifest.completeness != "complete"
            or manifest.truncated
            or bool(manifest.failed_units)
            for manifest in coverage_manifests
        )
        status: ScannerExecutionStatus = "partial" if partial else "success"
        extractor_version = next(
            (
                str(item.extractor_version)
                for item in (*artifacts, *observations)
                if str(item.extractor_version)
            ),
            "",
        )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                """
                SELECT id, status, finished_at FROM scanner_executions
                WHERE candidate_id=? AND company_key=? AND scanner_id=?
                  AND scanner_version=? AND scope_key=? AND config_hash=?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    normalized_candidate_id,
                    normalized_company,
                    scanner_id,
                    scanner_version,
                    scope_key,
                    config_hash,
                ),
            ).fetchone()
            if existing is not None:
                self.connection.commit()
                return {
                    "execution_id": int(existing["id"]),
                    "status": str(existing["status"]),
                    "company_key": normalized_company,
                    "reused": True,
                    "finished_at": existing["finished_at"],
                }

            cursor = self.connection.execute(
                """
                INSERT INTO scanner_executions(
                    candidate_id, run_id, company_key, scanner_id,
                    scanner_version, extractor_version, scope_key, config_hash,
                    status, started_at, summary_json, recorded_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'running', ?, '{}', ?)
                """,
                (
                    normalized_candidate_id,
                    normalized_company,
                    scanner_id,
                    scanner_version,
                    extractor_version[:120],
                    scope_key,
                    config_hash,
                    recorded_at,
                    recorded_at,
                ),
            )
            execution_id = int(cursor.lastrowid)
            execution = self.connection.execute(
                "SELECT * FROM scanner_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if execution is None:  # pragma: no cover - guarded by the insert
                raise RuntimeError("Market projection execution could not be read back")
            coverage_count, artifact_count, observation_count = (
                self._persist_scanner_evidence_uncommitted(
                    execution,
                    coverage_manifests=coverage_manifests,
                    artifacts=artifacts,
                    observations=observations,
                    lineage=(),
                    recorded_at=recorded_at,
                )
            )
            summary = ScannerExecutionSummary(
                execution_id=execution_id,
                candidate_id=normalized_candidate_id,
                company_key=normalized_company,
                scanner_id=scanner_id,
                scanner_version=scanner_version,
                extractor_version=extractor_version,
                scope_key=scope_key,
                config_hash=config_hash,
                status=status,
                started_at=now,
                finished_at=now,
                artifact_keys=[item.artifact_key for item in artifacts][:100],
                coverage_keys=[item.manifest_key for item in coverage_manifests][:100],
                observation_count=observation_count,
                metadata={
                    "execution_class": "market_stream_projection_v1",
                    "source_id": normalized_source,
                    "source_run_id": source_run_id,
                    "company_scan_clock_untouched": True,
                },
            )
            updated = self.connection.execute(
                """
                UPDATE scanner_executions
                SET status=?, finished_at=?, artifact_count=?, coverage_count=?,
                    observation_count=?, summary_json=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    recorded_at,
                    artifact_count,
                    coverage_count,
                    observation_count,
                    summary.model_dump_json(),
                    execution_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Market projection execution completion was not atomic")
            self.connection.commit()
            return {
                "execution_id": execution_id,
                "status": status,
                "company_key": normalized_company,
                "coverage_count": coverage_count,
                "artifact_count": artifact_count,
                "observation_count": observation_count,
                "reused": False,
                "finished_at": recorded_at,
            }
        except Exception:
            self.connection.rollback()
            raise

    def complete_scanner_execution(
        self,
        execution_id: int,
        summary: ScannerExecutionSummary,
        *,
        coverage_manifests: list[ScannerCoverageManifest]
        | tuple[ScannerCoverageManifest, ...] = (),
        artifacts: list[EvidenceArtifact] | tuple[EvidenceArtifact, ...] = (),
        observations: list[AtomicObservation] | tuple[AtomicObservation, ...] = (),
        lineage: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        next_due_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Commit a reusable scanner result and all of its evidence atomically."""
        if summary.status not in SCANNER_REUSABLE_STATUSES:
            raise ValueError("Complete scanner status must be reusable")
        if summary.execution_id not in {None, int(execution_id)}:
            raise ValueError("Scanner summary execution id does not match")
        if summary.status in {"success_empty", "not_applicable"} and observations:
            raise ValueError(f"{summary.status} cannot contain observations")
        if summary.status == "success" and (
            not coverage_manifests
            or any(
                manifest.completeness not in {"complete", "not_applicable"}
                or manifest.truncated
                or manifest.failed_units
                for manifest in coverage_manifests
            )
        ):
            raise ValueError("success requires complete, failure-free coverage")
        if summary.status in {"success_empty", "not_applicable"}:
            safe_coverage = bool(coverage_manifests) and all(
                manifest.absence_interpretable
                and not manifest.truncated
                and not manifest.failed_units
                and manifest.completeness in {"complete", "not_applicable"}
                for manifest in coverage_manifests
            )
            if not safe_coverage:
                raise ValueError(f"{summary.status} requires complete, failure-free coverage")
        if summary.status == "not_applicable" and not any(
            manifest.completeness == "not_applicable" for manifest in coverage_manifests
        ):
            raise ValueError("not_applicable requires an explicit coverage manifest")

        now = datetime.now(UTC)
        recorded_at = now.isoformat()
        # Freshness starts when the durable transaction completes, not from a
        # caller-controlled or source-reported timestamp.
        finished_at = now
        normalized_due = next_due_at or summary.next_due_at
        if normalized_due is not None:
            normalized_due = self._utc_datetime(normalized_due)
        hard_due = finished_at + REHYDRATION_INTERVAL
        if normalized_due is None or normalized_due < hard_due:
            normalized_due = hard_due

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            execution = self.connection.execute(
                "SELECT * FROM scanner_executions WHERE id=?",
                (int(execution_id),),
            ).fetchone()
            if execution is None:
                raise ValueError("Scanner execution does not exist")
            if execution["status"] != "running":
                raise ValueError("Scanner execution is no longer running")
            self._assert_scanner_payload_identity(
                execution,
                scanner_id=summary.scanner_id,
                scanner_version=summary.scanner_version,
                company_key=summary.company_key,
            )
            if summary.candidate_id not in {None, int(execution["candidate_id"] or 0)}:
                raise ValueError("Scanner summary candidate id does not match")
            coverage_count, artifact_count, observation_count = (
                self._persist_scanner_evidence_uncommitted(
                    execution,
                    coverage_manifests=coverage_manifests,
                    artifacts=artifacts,
                    observations=observations,
                    lineage=lineage,
                    recorded_at=recorded_at,
                )
            )
            normalized_summary = summary.model_copy(
                update={
                    "execution_id": int(execution_id),
                    "candidate_id": execution["candidate_id"],
                    "started_at": datetime.fromisoformat(execution["started_at"]),
                    "finished_at": finished_at,
                    "next_due_at": normalized_due,
                    "extractor_version": (
                        summary.extractor_version or execution["extractor_version"]
                    ),
                    "scope_key": summary.scope_key or execution["scope_key"],
                    "config_hash": summary.config_hash or execution["config_hash"],
                    "artifact_keys": [item.artifact_key for item in artifacts],
                    "coverage_keys": [item.manifest_key for item in coverage_manifests],
                    "observation_count": observation_count,
                }
            )
            cursor = self.connection.execute(
                """
                UPDATE scanner_executions
                SET status=?, finished_at=?, next_due_at=?, request_count=?, byte_count=?,
                    token_count=?, cost_microusd=?, artifact_count=?,
                    coverage_count=?, observation_count=?, summary_json=?, error=?
                WHERE id=? AND status='running'
                """,
                (
                    normalized_summary.status,
                    finished_at.isoformat(),
                    normalized_due.isoformat(),
                    normalized_summary.request_count,
                    normalized_summary.byte_count,
                    normalized_summary.token_count,
                    normalized_summary.cost_microusd,
                    artifact_count,
                    coverage_count,
                    observation_count,
                    normalized_summary.model_dump_json(),
                    "\n".join(normalized_summary.errors)[:2_000],
                    int(execution_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Scanner execution completion lost its claim")
            work = self.connection.execute(
                """
                UPDATE scanner_work
                SET status=?, claimed_candidate_id=NULL,
                    claimed_execution_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL,
                    completed_candidate_id=?, completed_execution_id=?,
                    completed_at=?, next_due_at=?, last_error='', updated_at=?
                WHERE company_key=? AND scanner_id=? AND scanner_version=?
                  AND status='running' AND claimed_execution_id=?
                """,
                (
                    normalized_summary.status,
                    execution["candidate_id"],
                    int(execution_id),
                    finished_at.isoformat(),
                    normalized_due.isoformat(),
                    recorded_at,
                    execution["company_key"],
                    execution["scanner_id"],
                    execution["scanner_version"],
                    int(execution_id),
                ),
            )
            if work.rowcount != 1:
                raise RuntimeError("Scanner work lease changed before completion")
            self.connection.commit()
            return {
                "execution_id": int(execution_id),
                "status": normalized_summary.status,
                "company_key": str(execution["company_key"]),
                "coverage_count": coverage_count,
                "artifact_count": artifact_count,
                "observation_count": observation_count,
                "completed_at": finished_at.isoformat(),
                "next_due_at": normalized_due.isoformat(),
            }
        except Exception:
            self.connection.rollback()
            raise

    def release_run_scanner_claims(self, run_id: int, reason: str) -> int:
        """Settle interrupted claims only after the caller has stopped its owned worker.

        Keep original evidence/accounting and existing failure retry floors. Joining
        from the small work ledger avoids reading historical execution payloads.
        """
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        minimum_due = now + timedelta(
            seconds=SCANNER_FAILURE_DEFAULT_RETRY_SECONDS["transient_error"],
        )
        error = reason.strip()[:2_000]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """
                SELECT execution.* FROM scanner_work work
                CROSS JOIN scanner_executions execution ON execution.id=work.claimed_execution_id
                WHERE work.status='running' AND execution.status='running' AND execution.run_id=?
                  AND execution.company_key=work.company_key
                  AND execution.scanner_id=work.scanner_id
                  AND execution.scanner_version=work.scanner_version
                """,
                (int(run_id),),
            ).fetchall()
            for row in rows:
                summary = self._scanner_summary_from_row(row)
                next_due = max(
                    minimum_due, summary.next_due_at or minimum_due,
                    self._utc_datetime(row["next_due_at"]) if row["next_due_at"] else minimum_due,
                )
                summary = summary.model_copy(update={
                    "status": "transient_error", "finished_at": now, "next_due_at": next_due,
                    "request_count": row["request_count"], "byte_count": row["byte_count"],
                    "token_count": row["token_count"], "cost_microusd": row["cost_microusd"],
                    "observation_count": row["observation_count"],
                    "errors": list(dict.fromkeys(
                        item for item in [*summary.errors, row["error"], error] if item
                    )),
                })
                execution_update = self.connection.execute(
                    """
                    UPDATE scanner_executions
                    SET status='transient_error', finished_at=?, next_due_at=?, summary_json=?,
                        error=? WHERE id=? AND status='running' AND run_id=?
                    """,
                    (timestamp, next_due.isoformat(), summary.model_dump_json(), error,
                     row["id"], int(run_id)),
                )
                work_update = self.connection.execute(
                    """
                    UPDATE scanner_work
                    SET status='transient_error', claimed_candidate_id=NULL,
                        claimed_execution_id=NULL, claimed_at=NULL, lease_expires_at=NULL,
                        next_due_at=?, last_error=?, updated_at=?
                    WHERE company_key=? AND scanner_id=? AND scanner_version=?
                      AND status='running' AND claimed_execution_id=?
                    """,
                    (next_due.isoformat(), error, timestamp, row["company_key"], row["scanner_id"],
                     row["scanner_version"], row["id"]),
                )
                if execution_update.rowcount != 1 or work_update.rowcount != 1:
                    raise RuntimeError("Interrupted scanner ownership changed before release")
            self.connection.commit()
            return len(rows)
        except BaseException:
            self.connection.rollback()
            raise

    def fail_scanner_execution(
        self,
        execution_id: int,
        *,
        status: ScannerExecutionStatus,
        error: str,
        summary: ScannerExecutionSummary | None = None,
        coverage_manifests: list[ScannerCoverageManifest]
        | tuple[ScannerCoverageManifest, ...] = (),
        artifacts: list[EvidenceArtifact] | tuple[EvidenceArtifact, ...] = (),
        observations: list[AtomicObservation] | tuple[AtomicObservation, ...] = (),
        lineage: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        next_due_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Release one failed/partial scanner independently, retaining partial evidence."""
        if status not in SCANNER_FAILURE_STATUSES:
            raise ValueError("Failure status must be retryable or terminal-failed")
        if summary is not None and summary.status != status:
            raise ValueError("Scanner failure summary status does not match")
        if summary is not None and summary.execution_id not in {None, int(execution_id)}:
            raise ValueError("Scanner failure summary execution id does not match")
        normalized_error = error.strip()[:2_000]
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        planned_due_at = next_due_at or (summary.next_due_at if summary is not None else None)
        minimum_due_at = now + timedelta(seconds=SCANNER_FAILURE_DEFAULT_RETRY_SECONDS[status])
        if planned_due_at is None:
            next_due_at = minimum_due_at
        else:
            next_due_at = max(self._utc_datetime(planned_due_at), minimum_due_at)

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            execution = self.connection.execute(
                "SELECT * FROM scanner_executions WHERE id=?",
                (int(execution_id),),
            ).fetchone()
            if execution is None:
                raise ValueError("Scanner execution does not exist")
            if execution["status"] != "running":
                raise ValueError("Scanner execution is no longer running")
            if summary is not None:
                self._assert_scanner_payload_identity(
                    execution,
                    scanner_id=summary.scanner_id,
                    scanner_version=summary.scanner_version,
                    company_key=summary.company_key,
                )
            coverage_count, artifact_count, observation_count = (
                self._persist_scanner_evidence_uncommitted(
                    execution,
                    coverage_manifests=coverage_manifests,
                    artifacts=artifacts,
                    observations=observations,
                    lineage=lineage,
                    recorded_at=timestamp,
                )
            )
            normalized_summary = (
                summary.model_copy(
                    update={
                        "execution_id": int(execution_id),
                        "candidate_id": execution["candidate_id"],
                        "started_at": datetime.fromisoformat(execution["started_at"]),
                        "finished_at": now,
                        "next_due_at": next_due_at,
                        "extractor_version": (
                            summary.extractor_version or execution["extractor_version"]
                        ),
                        "scope_key": summary.scope_key or execution["scope_key"],
                        "config_hash": summary.config_hash or execution["config_hash"],
                        "artifact_keys": [item.artifact_key for item in artifacts],
                        "coverage_keys": [item.manifest_key for item in coverage_manifests],
                        "observation_count": observation_count,
                        "errors": [*summary.errors, normalized_error]
                        if normalized_error
                        else summary.errors,
                    }
                )
                if summary is not None
                else ScannerExecutionSummary(
                    execution_id=int(execution_id),
                    candidate_id=execution["candidate_id"],
                    company_key=execution["company_key"],
                    scanner_id=execution["scanner_id"],
                    scanner_version=execution["scanner_version"],
                    extractor_version=execution["extractor_version"],
                    scope_key=execution["scope_key"],
                    config_hash=execution["config_hash"],
                    status=status,
                    started_at=datetime.fromisoformat(execution["started_at"]),
                    finished_at=now,
                    next_due_at=next_due_at,
                    artifact_keys=[item.artifact_key for item in artifacts],
                    coverage_keys=[item.manifest_key for item in coverage_manifests],
                    observation_count=observation_count,
                    errors=[normalized_error] if normalized_error else [],
                )
            )
            cursor = self.connection.execute(
                """
                UPDATE scanner_executions
                SET status=?, finished_at=?, next_due_at=?, request_count=?, byte_count=?,
                    token_count=?, cost_microusd=?, artifact_count=?,
                    coverage_count=?, observation_count=?, summary_json=?, error=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    timestamp,
                    next_due_at.isoformat() if next_due_at else None,
                    normalized_summary.request_count,
                    normalized_summary.byte_count,
                    normalized_summary.token_count,
                    normalized_summary.cost_microusd,
                    artifact_count,
                    coverage_count,
                    observation_count,
                    normalized_summary.model_dump_json(),
                    normalized_error,
                    int(execution_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Scanner failure lost its execution claim")
            work = self.connection.execute(
                """
                UPDATE scanner_work
                SET status=?, claimed_candidate_id=NULL,
                    claimed_execution_id=NULL, claimed_at=NULL,
                    lease_expires_at=NULL, next_due_at=?, last_error=?, updated_at=?
                WHERE company_key=? AND scanner_id=? AND scanner_version=?
                  AND status='running' AND claimed_execution_id=?
                """,
                (
                    status,
                    next_due_at.isoformat() if next_due_at else None,
                    normalized_error,
                    timestamp,
                    execution["company_key"],
                    execution["scanner_id"],
                    execution["scanner_version"],
                    int(execution_id),
                ),
            )
            if work.rowcount != 1:
                raise RuntimeError("Scanner work lease changed before failure")
            self.connection.commit()
            return {
                "execution_id": int(execution_id),
                "status": status,
                "company_key": str(execution["company_key"]),
                "coverage_count": coverage_count,
                "artifact_count": artifact_count,
                "observation_count": observation_count,
                "retry_after": next_due_at.isoformat() if next_due_at else None,
            }
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _coverage_manifest_from_row(row: sqlite3.Row) -> ScannerCoverageManifest:
        return ScannerCoverageManifest(
            execution_id=row["execution_id"],
            manifest_key=row["manifest_key"],
            scanner_id=row["scanner_id"],
            scanner_version=row["scanner_version"],
            surface=row["surface"],
            scope=row["scope"],
            window_start=row["window_start"],
            window_end=row["window_end"],
            attempted_units=row["attempted_units"],
            successful_units=row["successful_units"],
            failed_units=row["failed_units"],
            total_units=row["total_units"],
            completeness=row["completeness"],
            truncated=bool(row["truncated"]),
            absence_interpretable=bool(row["absence_interpretable"]),
            limitations=json.loads(row["limitations_json"] or "[]"),
            source_urls=json.loads(row["source_urls_json"] or "[]"),
            observed_at=row["observed_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _evidence_artifact_from_row(row: sqlite3.Row) -> EvidenceArtifact:
        return EvidenceArtifact(
            execution_id=row["execution_id"],
            artifact_key=row["artifact_key"],
            company_key=row["company_key"],
            scanner_id=row["scanner_id"],
            scanner_version=row["scanner_version"],
            extractor_version=row["extractor_version"],
            artifact_type=row["artifact_type"],
            canonical_url=row["canonical_url"],
            original_url=row["original_url"],
            publisher=row["publisher"],
            fetched_at=row["fetched_at"],
            status_code=row["status_code"],
            mime_type=row["mime_type"],
            content_hash=row["content_hash"],
            published_at=row["published_at"],
            published_time_basis=row["published_time_basis"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            byte_count=row["byte_count"],
            robots_state=row["robots_state"],
            truncated=bool(row["truncated"]),
            excerpt=row["excerpt"],
            locator=row["locator"],
            retention_policy=row["retention_policy"],
            license_policy=row["license_policy"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _atomic_observation_from_row(row: sqlite3.Row) -> AtomicObservation:
        return AtomicObservation(
            execution_id=row["execution_id"],
            observation_key=row["observation_key"],
            company_key=row["company_key"],
            scanner_id=row["scanner_id"],
            scanner_version=row["scanner_version"],
            extractor_version=row["extractor_version"],
            rule_id=row["rule_id"],
            rule_version=row["rule_version"],
            entity_key=row["entity_key"],
            predicate=row["predicate"],
            value_type=row["value_type"],
            value=json.loads(row["value_json"]),
            unit=row["unit"],
            qualifiers=json.loads(row["qualifiers_json"] or "{}"),
            summary=row["summary"],
            source_url=row["source_url"],
            epistemic_status=row["epistemic_status"],
            validity_status=row["validity_status"],
            confidence=row["confidence"],
            confidence_method=row["confidence_method"],
            first_observed_at=row["first_observed_at"],
            last_observed_at=row["last_observed_at"],
            effective_at=row["effective_at"],
            effective_time_basis=row["effective_time_basis"],
            expires_at=row["expires_at"],
            artifact_keys=json.loads(row["artifact_keys_json"] or "[]"),
            coverage_keys=json.loads(row["coverage_keys_json"] or "[]"),
            score_eligible=bool(row["score_eligible"]),
            discovery_only=bool(row["discovery_only"]),
            source_actor_key=row["source_actor_key"],
            origin_fingerprint=row["origin_fingerprint"],
            causal_root_key=row["causal_root_key"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _scanner_summary_from_row(row: sqlite3.Row) -> ScannerExecutionSummary:
        if row["summary_json"] and row["summary_json"] != "{}":
            try:
                return ScannerExecutionSummary.model_validate_json(row["summary_json"])
            except (TypeError, ValueError):
                pass
        return ScannerExecutionSummary(
            execution_id=row["id"],
            candidate_id=row["candidate_id"],
            company_key=row["company_key"],
            scanner_id=row["scanner_id"],
            scanner_version=row["scanner_version"],
            extractor_version=row["extractor_version"],
            scope_key=row["scope_key"],
            config_hash=row["config_hash"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            next_due_at=row["next_due_at"],
            request_count=row["request_count"],
            byte_count=row["byte_count"],
            token_count=row["token_count"],
            cost_microusd=row["cost_microusd"],
            observation_count=row["observation_count"],
            errors=[row["error"]] if row["error"] else [],
        )

    @staticmethod
    def _scanner_schedule_decision_from_row(
        row: sqlite3.Row,
    ) -> ScannerScheduleDecision:
        return ScannerScheduleDecision(
            company_key=row["company_key"],
            scanner_id=row["scanner_id"],
            scanner_version=row["scanner_version"],
            decided_at=row["decided_at"],
            applicable=bool(row["applicable"]),
            applicability_reason=row["applicability_reason"],
            current_action=row["current_action"],
            success_probability=row["success_probability"],
            expected_action_entropy_reduction=row["expected_action_entropy_reduction"],
            action_boundary_sensitivity=row["action_boundary_sensitivity"],
            evidence_value=row["evidence_value"],
            cost_penalty=row["cost_penalty"],
            rate_penalty=row["rate_penalty"],
            identity_penalty=row["identity_penalty"],
            risk_penalty=row["risk_penalty"],
            expected_information_gain=row["expected_information_gain"],
            selected=bool(row["selected"]),
            selection_reason=row["selection_reason"],
            exploration=bool(row["exploration"]),
            propensity=row["propensity"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def scanner_execution(
        self,
        execution_id: int,
        *,
        include_evidence: bool = True,
    ) -> dict[str, Any] | None:
        """Return one execution and optionally its immutable evidence ledger."""
        row = self.connection.execute(
            "SELECT * FROM scanner_executions WHERE id=?",
            (int(execution_id),),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "summary": self._scanner_summary_from_row(row),
            "recorded_at": row["recorded_at"],
        }
        if not include_evidence:
            return result
        result["coverage_manifests"] = [
            self._coverage_manifest_from_row(item)
            for item in self.connection.execute(
                """
                SELECT * FROM coverage_manifests
                WHERE execution_id=? ORDER BY id
                """,
                (int(execution_id),),
            ).fetchall()
        ]
        result["evidence_artifacts"] = [
            self._evidence_artifact_from_row(item)
            for item in self.connection.execute(
                """
                SELECT * FROM evidence_artifacts
                WHERE execution_id=? ORDER BY id
                """,
                (int(execution_id),),
            ).fetchall()
        ]
        result["observations"] = [
            self._atomic_observation_from_row(item)
            for item in self.connection.execute(
                """
                SELECT * FROM atomic_observations
                WHERE execution_id=? ORDER BY id
                """,
                (int(execution_id),),
            ).fetchall()
        ]
        result["lineage"] = [
            {
                **dict(item),
                "details": json.loads(item["details_json"] or "{}"),
            }
            for item in self.connection.execute(
                """
                SELECT * FROM observation_lineage
                WHERE execution_id=? ORDER BY id
                """,
                (int(execution_id),),
            ).fetchall()
        ]
        return result

    def scanner_work_states(
        self,
        candidate_ids: list[int],
        *,
        scanner_id: str | None = None,
        scanner_version: str | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        """Project scanner work onto every requested canonical candidate alias."""
        identifiers = self._normalized_candidate_ids(candidate_ids)
        company_keys = self._candidate_company_keys(identifiers)
        unique_keys = list(dict.fromkeys(company_keys.values()))
        by_company: dict[str, list[dict[str, Any]]] = {key: [] for key in unique_keys}
        for start in range(0, len(unique_keys), 500):
            batch = unique_keys[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            conditions = [f"company_key IN ({placeholders})"]
            params: list[Any] = [*batch]
            if scanner_id is not None:
                conditions.append("scanner_id=?")
                params.append(scanner_id.strip())
            if scanner_version is not None:
                conditions.append("scanner_version=?")
                params.append(scanner_version.strip())
            rows = self.connection.execute(
                f"""
                SELECT * FROM scanner_work
                WHERE {" AND ".join(conditions)}
                ORDER BY scanner_id, scanner_version
                """,
                params,
            ).fetchall()
            for row in rows:
                by_company.setdefault(str(row["company_key"]), []).append(dict(row))
        return {
            candidate_id: list(by_company.get(company_key, []))
            for candidate_id in identifiers
            if (company_key := company_keys.get(candidate_id)) is not None
        }

    def atomic_observations(
        self,
        company_key: str,
        *,
        as_of: datetime | None = None,
        scanner_ids: list[str] | None = None,
        score_eligible_only: bool = False,
    ) -> list[AtomicObservation]:
        """Read observations known by an optional leakage-safe recording cutoff."""
        normalized_company = self._normalized_company_key(company_key)
        conditions = ["company_key=?"]
        params: list[Any] = [normalized_company]
        if as_of is not None:
            cutoff = self._utc_datetime(as_of)
            conditions.extend(["recorded_at<=?", "first_observed_at<=?"])
            params.extend([cutoff.isoformat(), cutoff.isoformat()])
        normalized_scanners = list(
            dict.fromkeys(item.strip() for item in (scanner_ids or []) if item.strip())
        )
        if normalized_scanners:
            placeholders = ", ".join("?" for _ in normalized_scanners)
            conditions.append(f"scanner_id IN ({placeholders})")
            params.extend(normalized_scanners)
        if score_eligible_only:
            conditions.append("score_eligible=1")
        rows = self.connection.execute(
            f"""
            SELECT * FROM atomic_observations
            WHERE {" AND ".join(conditions)}
            ORDER BY recorded_at, id
            """,
            params,
        ).fetchall()
        return [self._atomic_observation_from_row(row) for row in rows]

    def observation_lineage(
        self,
        company_key: str,
        *,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read lineage edges only when their subject was recorded by the cutoff."""
        normalized_company = self._normalized_company_key(company_key)
        conditions = ["ao.company_key=?"]
        params: list[Any] = [normalized_company]
        if as_of is not None:
            cutoff = self._utc_datetime(as_of)
            conditions.extend(["ol.created_at<=?", "ao.recorded_at<=?"])
            params.extend([cutoff.isoformat(), cutoff.isoformat()])
        rows = self.connection.execute(
            f"""
            SELECT ol.* FROM observation_lineage ol
            JOIN atomic_observations ao
              ON ao.execution_id=ol.execution_id
             AND ao.observation_key=ol.subject_observation_key
            WHERE {" AND ".join(conditions)}
            ORDER BY ol.created_at, ol.id
            """,
            params,
        ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"] or "{}")} for row in rows]

    def record_scanner_schedule_decision(
        self,
        decision: ScannerScheduleDecision,
        *,
        candidate_id: int | None = None,
        execution_id: int | None = None,
    ) -> int:
        """Append one auditable EIG/applicability/propensity decision."""
        normalized_company = self._normalized_company_key(decision.company_key)
        require_allowed(normalized_company)
        if candidate_id is not None:
            expected = self._candidate_company_keys([candidate_id]).get(int(candidate_id))
            if expected is None:
                raise ValueError("Schedule decision candidate does not exist")
            if expected != normalized_company:
                raise ValueError("Schedule decision candidate identity does not match")
        if execution_id is not None:
            execution = self.connection.execute(
                "SELECT * FROM scanner_executions WHERE id=?",
                (int(execution_id),),
            ).fetchone()
            if execution is None:
                raise ValueError("Schedule decision execution does not exist")
            self._assert_scanner_payload_identity(
                execution,
                scanner_id=decision.scanner_id,
                scanner_version=decision.scanner_version,
                company_key=normalized_company,
            )
        recorded_at = _now()
        decision_payload = decision.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            INSERT INTO scanner_schedule_decisions(
                candidate_id, execution_id, company_key, scanner_id,
                scanner_version, decided_at, applicable,
                applicability_reason, current_action, success_probability,
                expected_action_entropy_reduction, action_boundary_sensitivity,
                evidence_value, cost_penalty, rate_penalty, identity_penalty,
                risk_penalty, expected_information_gain, selected,
                selection_reason, exploration, propensity, metadata_json,
                recorded_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                candidate_id,
                execution_id,
                normalized_company,
                decision.scanner_id,
                decision.scanner_version,
                self._frontier_timestamp(decision.decided_at),
                int(decision.applicable),
                decision.applicability_reason,
                decision.current_action,
                decision.success_probability,
                decision.expected_action_entropy_reduction,
                decision.action_boundary_sensitivity,
                decision.evidence_value,
                decision.cost_penalty,
                decision.rate_penalty,
                decision.identity_penalty,
                decision.risk_penalty,
                decision.expected_information_gain,
                int(decision.selected),
                decision.selection_reason,
                int(decision.exploration),
                decision.propensity,
                encode_storage_json(json.dumps(decision_payload["metadata"], sort_keys=True)),
                recorded_at,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    @classmethod
    def _dataforseo_trajectory_feature(
        cls,
        rows: list[sqlite3.Row],
        *,
        as_of: datetime,
    ) -> dict[str, Any] | None:
        """Derive a bounded, positive-only search-emergence feature as of one cutoff.

        The feature is deliberately not an atomic observation: it is a deterministic comparison
        of two immutable provider observations. Keeping it in the feature projection prevents an
        older sample from being mistaken for a current fact while retaining leakage-safe history.
        """

        supported = {
            "seo.search_visibility_estimate": False,
            "seo.domain_rank_overview": True,
        }
        series: dict[str, list[dict[str, Any]]] = {predicate: [] for predicate in supported}
        cutoff = cls._utc_datetime(as_of)
        for row in rows:
            if str(row["scanner_id"]) != DATAFORSEO_SCANNER_ID:
                continue
            predicate = str(row["predicate"])
            if predicate not in supported:
                continue
            try:
                value = json.loads(row["value_json"] or "{}")
                qualifiers = json.loads(row["qualifiers_json"] or "{}")
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not all(isinstance(item, dict) for item in (value, qualifiers, metadata)):
                continue
            organic = value.get("organic")
            count = organic.get("count") if isinstance(organic, dict) else None
            etv = organic.get("etv") if isinstance(organic, dict) else None
            observed_at = cls._datetime_or_none(row["last_observed_at"])
            detail = supported[predicate]
            target = str(value.get("target") or "").strip().lower().removeprefix("www.")
            company = str(row["company_key"] or "").strip().lower().removeprefix("www.")
            if (
                observed_at is None
                or observed_at > cutoff
                or not target
                or target != company
                or value.get("provider_estimate") is not True
                or value.get("data_available") is not True
                or value.get("location_code") != 2840
                or value.get("language_code") != "en"
                or qualifiers.get("score_policy")
                != "dataforseo_search_visibility_capacity_v1"
                or qualifiers.get("provider_estimate") is not True
                or qualifiers.get("traffic_is_estimated") is not True
                or qualifiers.get("first_party_analytics") is not False
                or qualifiers.get("coverage_complete") is not True
                or qualifiers.get("clickstream_included") is not False
                or qualifiers.get("rank_measured") is not detail
                or metadata.get("projection_score_gate")
                != "exact_dataforseo_search_visibility"
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or isinstance(etv, bool)
                or not isinstance(etv, (int, float))
                or not math.isfinite(float(etv))
                or float(etv) < 0
            ):
                continue
            series[predicate].append(
                {
                    "observed_at": observed_at,
                    "organic_count": count,
                    "organic_etv": float(etv),
                }
            )

        selected_predicate = ""
        samples: list[dict[str, Any]] = []
        for predicate in ("seo.domain_rank_overview", "seo.search_visibility_estimate"):
            unique: dict[str, dict[str, Any]] = {}
            for sample in sorted(
                series[predicate],
                key=lambda item: item["observed_at"],
                reverse=True,
            ):
                unique.setdefault(sample["observed_at"].isoformat(), sample)
            if len(unique) >= 2:
                selected_predicate = predicate
                samples = list(unique.values())[:2]
                break
        if len(samples) < 2:
            return None

        current, previous = samples
        interval_days = (current["observed_at"] - previous["observed_at"]).total_seconds() / 86_400
        current_age_days = (cutoff - current["observed_at"]).total_seconds() / 86_400
        if (
            not 21 <= interval_days <= 120
            or current_age_days < 0
            or current_age_days >= DATAFORSEO_EVIDENCE_DAYS
        ):
            return None

        count_delta = int(current["organic_count"]) - int(previous["organic_count"])
        etv_delta = float(current["organic_etv"]) - float(previous["organic_etv"])
        count_growth = count_delta / max(int(previous["organic_count"]), 25)
        etv_growth = etv_delta / max(float(previous["organic_etv"]), 100.0)
        count_emergent = (
            int(current["organic_count"]) >= 25
            and count_delta >= 10
            and count_growth >= 0.25
        )
        etv_emergent = (
            float(current["organic_etv"]) >= 100
            and etv_delta >= 100
            and etv_growth >= 0.30
        )
        points = 0
        if count_emergent:
            points += 1
            if count_delta >= 50 and count_growth >= 0.75:
                points += 1
        if etv_emergent:
            points += 1
            if etv_delta >= 500 and etv_growth >= 0.75:
                points += 1
        points = min(4, points)
        if points <= 0:
            return None
        return {
            "score_policy": DATAFORSEO_TRAJECTORY_SCORE_POLICY,
            "scanner_id": DATAFORSEO_SCANNER_ID,
            "predicate": selected_predicate,
            "sample_count": 2,
            "previous_observed_at": previous["observed_at"].isoformat(),
            "current_observed_at": current["observed_at"].isoformat(),
            "interval_days": round(interval_days, 3),
            "current_age_days": round(current_age_days, 3),
            "organic_count_previous": int(previous["organic_count"]),
            "organic_count_current": int(current["organic_count"]),
            "organic_count_delta": count_delta,
            "organic_count_growth": round(count_growth, 4),
            "organic_etv_previous": round(float(previous["organic_etv"]), 4),
            "organic_etv_current": round(float(current["organic_etv"]), 4),
            "organic_etv_delta": round(etv_delta, 4),
            "organic_etv_growth": round(etv_growth, 4),
            "count_emergent": count_emergent,
            "etv_emergent": etv_emergent,
            "points": points,
            "positive_only": True,
            "modeled_provider_estimates": True,
        }

    def _intelligence_feature_values_as_of(
        self,
        observation_rows: list[sqlite3.Row],
        execution_rows: list[sqlite3.Row],
        *,
        as_of: datetime,
    ) -> tuple[dict[str, Any], list[str], dict[str, datetime], set[str]]:
        """Resolve complete features once, without building unused export metadata."""
        empty_terminal_by_scanner: dict[str, datetime] = {}
        # All terminal coverage participates, including manifests recorded after the
        # feature cutoff, just as in the original per-execution lookup. Batch only
        # the reads; never turn missing/partial coverage into evidence of absence.
        empty_execution_ids = [
            int(row["id"])
            for row in execution_rows
            if str(row["status"]) in {"success_empty", "not_applicable"}
            and (finished := self._datetime_or_none(row["finished_at"])) is not None
            and finished <= as_of
        ]
        interpretable_empty: dict[int, bool] = {}
        for start in range(0, len(empty_execution_ids), 400):
            batch = empty_execution_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in self.connection.execute(
                f"""
                SELECT execution_id, completeness, truncated, failed_units,
                       absence_interpretable
                FROM coverage_manifests WHERE execution_id IN ({placeholders})
                """,
                batch,
            ):
                execution_id = int(row["execution_id"])
                valid = (
                    bool(row["absence_interpretable"])
                    and not bool(row["truncated"])
                    and not int(row["failed_units"] or 0)
                    and str(row["completeness"]) in {"complete", "not_applicable"}
                )
                interpretable_empty[execution_id] = (
                    interpretable_empty.get(execution_id, True) and valid
                )
        for execution_row in execution_rows:
            if str(execution_row["status"]) not in {"success_empty", "not_applicable"}:
                continue
            finished_at = self._datetime_or_none(execution_row["finished_at"])
            if finished_at is None or finished_at > as_of:
                continue
            if not interpretable_empty.get(int(execution_row["id"]), False):
                continue
            scanner_id = str(execution_row["scanner_id"])
            empty_terminal_by_scanner[scanner_id] = max(
                empty_terminal_by_scanner.get(
                    scanner_id,
                    datetime.min.replace(tzinfo=UTC),
                ),
                finished_at,
            )
        latest_observations: dict[tuple[str, str, str], sqlite3.Row] = {}
        for row in observation_rows:
            terminal_at = empty_terminal_by_scanner.get(str(row["scanner_id"]))
            recorded_at = self._datetime_or_none(row["recorded_at"])
            if terminal_at is not None and (
                recorded_at is None or terminal_at > recorded_at
            ):
                continue
            key = (
                str(row["scanner_id"]),
                str(row["entity_key"]),
                str(row["predicate"]),
            )
            latest_observations.setdefault(key, row)

        features: dict[str, Any] = {}
        observation_keys: list[str] = []
        latest_observed_by_scanner: dict[str, datetime] = {}
        scanners_with_observations: set[str] = set()
        for (scanner_id, entity_key, predicate), row in latest_observations.items():
            observation = self._atomic_observation_from_row(row)
            feature_key = f"{scanner_id}|{entity_key}|{predicate}"
            expires_at = (
                self._utc_datetime(observation.expires_at) if observation.expires_at else None
            )
            expired_at_cutoff = expires_at is not None and expires_at <= as_of
            features[feature_key] = {
                "observation_key": observation.observation_key,
                "scanner_id": scanner_id,
                "scanner_version": observation.scanner_version,
                "entity_key": entity_key,
                "predicate": predicate,
                "value_type": observation.value_type,
                "value": observation.value,
                "unit": observation.unit,
                "qualifiers": observation.qualifiers,
                "epistemic_status": observation.epistemic_status,
                "validity_status": (
                    "expired" if expired_at_cutoff else observation.validity_status
                ),
                "confidence": observation.confidence,
                "score_eligible": bool(observation.score_eligible and not expired_at_cutoff),
                "effective_at": observation.effective_at.isoformat()
                if observation.effective_at
                else None,
                "first_observed_at": observation.first_observed_at.isoformat(),
                "last_observed_at": observation.last_observed_at.isoformat(),
                "recorded_at": str(row["recorded_at"]),
                "source_actor_key": observation.source_actor_key,
                "origin_fingerprint": observation.origin_fingerprint,
                "causal_root_key": observation.causal_root_key,
            }
            observation_keys.append(observation.observation_key)
            scanners_with_observations.add(scanner_id)
            observed_at = self._utc_datetime(observation.last_observed_at)
            previous = latest_observed_by_scanner.get(scanner_id)
            if previous is None or observed_at > previous:
                latest_observed_by_scanner[scanner_id] = observed_at

        trajectory = self._dataforseo_trajectory_feature(
            observation_rows,
            as_of=as_of,
        )
        if trajectory is not None:
            features[DATAFORSEO_TRAJECTORY_FEATURE_KEY] = trajectory

        return features, observation_keys, latest_observed_by_scanner, scanners_with_observations

    def _frontier_queries(self, company_key, cutoff, execution_limit):
        normalized_company = self._normalized_company_key(company_key)
        cutoff_text = cutoff.isoformat()
        # Scoring requests the entire company ledger. Bounded display/API callers retain
        # their existing limit; scanner traffic must not evict another scanner's evidence.
        limit = -1 if execution_limit is None else max(1, min(int(execution_limit), 1_000))
        execution_query = """
            SELECT * FROM scanner_executions
            WHERE company_key=? AND recorded_at<=? AND started_at<=?
            ORDER BY started_at DESC, id DESC LIMIT ?
            """
        decision_query = """
            SELECT * FROM scanner_schedule_decisions
            WHERE company_key=? AND recorded_at<=? AND decided_at<=?
            ORDER BY decided_at, id
            LIMIT 1000
            """
        features_query = """
            SELECT * FROM atomic_observations
            WHERE company_key=? AND recorded_at<=? AND first_observed_at<=?
            ORDER BY recorded_at DESC, id DESC
            """
        parameters = (normalized_company, cutoff_text, cutoff_text)
        return (execution_query, (*parameters, limit)), (decision_query, parameters), (
            features_query, parameters,
        )

    def frontier_intelligence(
        self,
        company_key: str,
        *,
        as_of: datetime | None = None,
        execution_limit: int | None = 100,
    ) -> FrontierIntelligence:
        """Hydrate the typed frontier projection from the append-only ledgers."""
        cutoff = self._utc_datetime(as_of or datetime.now(UTC))
        cutoff_text = cutoff.isoformat()
        execution_query, decision_query, features_query = self._frontier_queries(
            company_key, cutoff, execution_limit,
        )
        prefetch = getattr(self.connection, "prefetch", None)
        if prefetch is not None:
            prefetch((execution_query, decision_query, features_query))
        execution_rows = self.connection.execute(*execution_query).fetchall()
        executions: list[ScannerExecutionSummary] = []
        execution_ids: list[int] = []
        for row in execution_rows:
            execution_ids.append(int(row["id"]))
            summary = self._scanner_summary_from_row(row)
            finished_at = self._datetime_or_none(row["finished_at"])
            if finished_at is None or finished_at > cutoff:
                summary = summary.model_copy(
                    update={
                        "status": "running",
                        "finished_at": None,
                        "next_due_at": None,
                        "request_count": 0,
                        "byte_count": 0,
                        "token_count": 0,
                        "cost_microusd": 0,
                        "artifact_keys": [],
                        "coverage_keys": [],
                        "observation_count": 0,
                        "errors": [],
                        "metadata": {},
                    }
                )
            executions.append(summary)

        placeholders = ", ".join("?" for _ in execution_ids)

        def evidence_query(table):
            # The execution/key unique index remains selective for long receipt histories.
            # Otherwise SQLite can skip-scan the entire recorded-at index for a large IN list.
            return (
                f"SELECT * FROM {table} INDEXED BY sqlite_autoindex_{table}_1 "
                f"WHERE execution_id IN ({placeholders}) "
                "AND recorded_at<=? ORDER BY recorded_at, id", (*execution_ids, cutoff_text),
            )

        def evidence_rows(table):
            return (self.connection.execute(*evidence_query(table)).fetchall()
                    if execution_ids else [])

        if prefetch is not None and execution_ids:
            prefetch(evidence_query(table) for table in
                     ("coverage_manifests", "evidence_artifacts", "atomic_observations"))

        coverage = [
            self._coverage_manifest_from_row(row)
            for row in evidence_rows("coverage_manifests")
            if str(row["observed_at"]) <= cutoff_text
        ]
        artifacts = [
            self._evidence_artifact_from_row(row) for row in evidence_rows("evidence_artifacts")
        ]
        observations = [
            self._atomic_observation_from_row(row)
            for row in evidence_rows("atomic_observations")
            if str(row["first_observed_at"]) <= cutoff_text
        ]
        decision_rows = self.connection.execute(*decision_query).fetchall()
        decisions = [self._scanner_schedule_decision_from_row(row) for row in decision_rows]
        feature_observations = self.connection.execute(*features_query).fetchall()
        # Display execution limits never limit scoring features. A full-ledger caller
        # can reuse its exact execution rows; bounded callers retain the complete read.
        feature_executions = execution_rows if execution_limit is None else self.connection.execute(
            """
            SELECT * FROM scanner_executions
            WHERE company_key=? AND recorded_at<=? AND started_at<=?
            ORDER BY started_at DESC, id DESC
            """,
            execution_query[1][:-1],
        ).fetchall()
        features, _, _, _ = self._intelligence_feature_values_as_of(
            feature_observations, feature_executions, as_of=cutoff,
        )
        return FrontierIntelligence(
            executions=executions,
            coverage_manifests=coverage,
            evidence_artifacts=artifacts,
            observations=observations,
            schedule_decisions=decisions,
            features=features,
            updated_at=cutoff,
        )

    def export_intelligence_features_as_of(
        self,
        cutoff: datetime,
        *,
        company_keys: list[str] | None = None,
        feature_version: str = "frontier-features-v1",
        persist: bool = False,
        action_shown: str = "",
    ) -> list[dict[str, Any]]:
        """Build point-in-time features without admitting late-recorded evidence."""
        as_of = self._utc_datetime(cutoff)
        as_of_text = as_of.isoformat()
        normalized_version = feature_version.strip()[:120]
        if not normalized_version:
            raise ValueError("Feature version cannot be blank")
        if company_keys is None:
            rows = self.connection.execute(
                """
                SELECT company_key FROM atomic_observations
                WHERE recorded_at<=? AND first_observed_at<=?
                UNION
                SELECT company_key FROM scanner_executions
                WHERE recorded_at<=? AND started_at<=?
                UNION
                SELECT company_key FROM scanner_schedule_decisions
                WHERE recorded_at<=? AND decided_at<=?
                ORDER BY company_key
                """,
                (
                    as_of_text,
                    as_of_text,
                    as_of_text,
                    as_of_text,
                    as_of_text,
                    as_of_text,
                ),
            ).fetchall()
            normalized_companies = [str(row["company_key"]) for row in rows]
        else:
            normalized_companies = list(
                dict.fromkeys(
                    self._normalized_company_key(company_key)
                    for company_key in company_keys
                    if company_key.strip()
                )
            )

        exports: list[dict[str, Any]] = []
        for company_key in normalized_companies:
            observation_rows = self.connection.execute(
                """
                SELECT * FROM atomic_observations
                WHERE company_key=? AND recorded_at<=? AND first_observed_at<=?
                ORDER BY recorded_at DESC, id DESC
                """,
                (company_key, as_of_text, as_of_text),
            ).fetchall()
            execution_rows = self.connection.execute(
                """
                SELECT * FROM scanner_executions
                WHERE company_key=? AND recorded_at<=? AND started_at<=?
                ORDER BY started_at DESC, id DESC
                """,
                (company_key, as_of_text, as_of_text),
            ).fetchall()
            (
                features, observation_keys, latest_observed_by_scanner, scanners_with_observations,
            ) = self._intelligence_feature_values_as_of(
                observation_rows, execution_rows, as_of=as_of,
            )

            scanner_states: dict[str, dict[str, Any]] = {}
            for row in execution_rows:
                state_key = f"{row['scanner_id']}@{row['scanner_version']}"
                if state_key in scanner_states:
                    continue
                finished_at = self._datetime_or_none(row["finished_at"])
                status = (
                    str(row["status"])
                    if finished_at is not None and finished_at <= as_of
                    else "running"
                )
                scanner_states[state_key] = {
                    "execution_id": int(row["id"]),
                    "status": status,
                    "started_at": str(row["started_at"]),
                    "finished_at": (
                        finished_at.isoformat()
                        if finished_at is not None and finished_at <= as_of
                        else None
                    ),
                }

            coverage_rows = self.connection.execute(
                """
                SELECT cm.* FROM coverage_manifests cm
                WHERE cm.company_key=? AND cm.recorded_at<=? AND cm.observed_at<=?
                ORDER BY cm.recorded_at DESC, cm.id DESC
                """,
                (company_key, as_of_text, as_of_text),
            ).fetchall()
            coverage: dict[str, dict[str, Any]] = {}
            for row in coverage_rows:
                coverage_key = f"{row['scanner_id']}|{row['surface']}"
                if coverage_key in coverage:
                    continue
                coverage[coverage_key] = self._coverage_manifest_from_row(row).model_dump(
                    mode="json"
                )

            decision_rows = self.connection.execute(
                """
                SELECT propensity, scanner_id, scanner_version, selected, applicable,
                       applicability_reason, expected_information_gain, current_action,
                       selection_reason, exploration, decided_at
                FROM scanner_schedule_decisions
                WHERE company_key=? AND recorded_at<=? AND decided_at<=?
                  AND decided_at=(
                      SELECT MAX(decided_at) FROM scanner_schedule_decisions
                      WHERE company_key=? AND recorded_at<=? AND decided_at<=?
                  )
                ORDER BY selected DESC, id DESC
                """,
                (
                    company_key,
                    as_of_text,
                    as_of_text,
                    company_key,
                    as_of_text,
                    as_of_text,
                ),
            ).fetchall()
            decision = decision_rows[0] if decision_rows else None
            scheduling_propensity = float(decision["propensity"]) if decision is not None else None
            latest_schedule_by_scanner = {str(row["scanner_id"]): row for row in decision_rows}

            latest_status_by_scanner: dict[str, str] = {}
            for state_key, state in scanner_states.items():
                scanner_id = state_key.split("@", 1)[0]
                latest_status_by_scanner.setdefault(scanner_id, str(state["status"]))
            missingness: dict[str, str] = {}
            for scanner_id in FRONTIER_SCANNER_IDS:
                status = latest_status_by_scanner.get(scanner_id)
                if scanner_id in scanners_with_observations:
                    missingness[scanner_id] = "observed"
                elif status == "success_empty":
                    missingness[scanner_id] = "known_empty"
                elif status == "not_applicable":
                    missingness[scanner_id] = "not_applicable"
                elif status == "running":
                    missingness[scanner_id] = "in_progress"
                elif status in SCANNER_FAILURE_STATUSES:
                    missingness[scanner_id] = "unknown_after_failure"
                elif status == "success":
                    missingness[scanner_id] = "success_without_current_fact"
                elif (
                    schedule := latest_schedule_by_scanner.get(scanner_id)
                ) is not None and not bool(schedule["applicable"]):
                    missingness[scanner_id] = "not_applicable_by_schedule"
                elif schedule is not None and bool(schedule["selected"]):
                    missingness[scanner_id] = "selected_without_execution"
                elif schedule is not None:
                    missingness[scanner_id] = "not_selected"
                else:
                    missingness[scanner_id] = "not_scanned"
            evidence_ages = {
                scanner_id: max(0.0, (as_of - observed_at).total_seconds() / 86_400)
                for scanner_id, observed_at in latest_observed_by_scanner.items()
                if observed_at <= as_of
            }
            exports.append(
                {
                    "company_key": company_key,
                    "as_of": as_of_text,
                    "feature_version": normalized_version,
                    "features": features,
                    "missingness": missingness,
                    "coverage": coverage,
                    "scanner_states": scanner_states,
                    "evidence_ages_days": evidence_ages,
                    "scheduling_propensity": scheduling_propensity,
                    "schedule_decision": dict(decision) if decision is not None else None,
                    "schedule_decisions": [dict(row) for row in decision_rows],
                    "action_shown": action_shown,
                    "observation_keys": observation_keys,
                }
            )

        if persist and exports:
            recorded_at = _now()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.executemany(
                    """
                    INSERT INTO intelligence_feature_snapshots(
                        company_key, as_of_at, feature_version, features_json,
                        missingness_json, coverage_json, scanner_states_json,
                        evidence_ages_json, scheduling_propensity, action_shown,
                        observation_keys_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item["company_key"],
                            item["as_of"],
                            item["feature_version"],
                            encode_storage_json(json.dumps(item["features"], sort_keys=True)),
                            encode_storage_json(json.dumps(item["missingness"], sort_keys=True)),
                            encode_storage_json(json.dumps(item["coverage"], sort_keys=True)),
                            encode_storage_json(json.dumps(item["scanner_states"], sort_keys=True)),
                            encode_storage_json(json.dumps(
                                item["evidence_ages_days"], sort_keys=True)),
                            item["scheduling_propensity"],
                            item["action_shown"],
                            encode_storage_json(json.dumps(
                                item["observation_keys"], sort_keys=True)),
                            recorded_at,
                        )
                        for item in exports
                    ],
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return exports

    def intelligence_feature_snapshots(
        self,
        company_key: str,
        *,
        as_of: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read persisted feature exports without recomputing or mutating them."""
        normalized_company = self._normalized_company_key(company_key)
        conditions = ["company_key=?"]
        params: list[Any] = [normalized_company]
        if as_of is not None:
            cutoff = self._utc_datetime(as_of)
            conditions.append("as_of_at<=?")
            params.append(cutoff.isoformat())
        params.append(max(1, min(int(limit), 10_000)))
        rows = self.connection.execute(
            f"""
            SELECT * FROM intelligence_feature_snapshots
            WHERE {" AND ".join(conditions)}
            ORDER BY as_of_at DESC, id DESC LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "company_key": row["company_key"],
                "as_of": row["as_of_at"],
                "feature_version": row["feature_version"],
                "features": json.loads(row["features_json"] or "{}"),
                "missingness": json.loads(row["missingness_json"] or "{}"),
                "coverage": json.loads(row["coverage_json"] or "{}"),
                "scanner_states": json.loads(row["scanner_states_json"] or "{}"),
                "evidence_ages_days": json.loads(row["evidence_ages_json"] or "{}"),
                "scheduling_propensity": row["scheduling_propensity"],
                "action_shown": row["action_shown"],
                "observation_keys": json.loads(row["observation_keys_json"] or "[]"),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _market_text(
        value: object,
        *,
        field: str,
        maximum: int,
        required: bool = False,
    ) -> str:
        if value is None:
            text = ""
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
        else:
            raise ValueError(f"{field} must be text or a scalar identifier")
        if required and not text:
            raise ValueError(f"{field} is required")
        if len(text) > maximum:
            if required:
                raise ValueError(f"{field} exceeds {maximum} characters")
            text = text[:maximum]
        return text

    @staticmethod
    def _market_json(value: object, *, field: str, maximum_bytes: int) -> str:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be JSON-serializable") from exc
        if len(payload.encode("utf-8")) > maximum_bytes:
            raise ValueError(f"{field} exceeds {maximum_bytes} bytes")
        return payload

    @staticmethod
    def _market_iso_timestamp(
        value: datetime | date | str | None,
        *,
        field: str,
        default: str | None = None,
    ) -> str | None:
        if value is None or value == "":
            return default
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
        else:
            raise ValueError(f"{field} must be an ISO-8601 timestamp")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _normalize_market_entity_name(value: object) -> str:
        """Normalize exact names without fuzzy matching or legal-suffix removal."""
        text = str(value or "").casefold()
        return " ".join("".join(char if char.isalnum() else " " for char in text).split())[:500]

    @staticmethod
    def _market_mapping(value: object, *, field: str) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")  # type: ignore[union-attr]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field} must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a JSON object")
        return dict(value)

    @classmethod
    def _market_document_fingerprint(
        cls,
        document: dict[str, Any],
        *,
        canonical_url: str,
        metadata_json: str,
    ) -> str:
        durable = {
            "canonical_url": canonical_url,
            "document_type": document.get("document_type", document.get("kind", "")),
            "title": document.get("title", ""),
            "summary": document.get("summary", document.get("excerpt", "")),
            "published_at": document.get("published_at"),
            "source_updated_at": document.get("source_updated_at", document.get("updated_at")),
            "source_policy_version": document.get("source_policy_version", ""),
            "metadata": json.loads(metadata_json),
        }
        encoded = cls._market_json(
            durable,
            field="document fingerprint",
            maximum_bytes=96_000,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _market_identity_for_candidate(
        self,
        candidate_id: int | None,
        company_key: str | None,
    ) -> tuple[int | None, str] | None:
        normalized_company = (
            self._normalized_company_key(company_key) if company_key and company_key.strip() else ""
        )
        if candidate_id is not None:
            try:
                normalized_candidate_id = int(candidate_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("candidate_id must be an integer") from exc
            resolved = self._candidate_company_keys([normalized_candidate_id]).get(
                normalized_candidate_id
            )
            if not resolved:
                raise ValueError("candidate_id does not identify a persisted candidate")
            if normalized_company and normalized_company != resolved:
                raise ValueError("company_key does not match the candidate's canonical company")
            return normalized_candidate_id, resolved
        if not normalized_company:
            return None

        rows = self.connection.execute(
            """
            WITH latest_target AS (
                SELECT ti.candidate_id, ti.canonical_domain,
                       ROW_NUMBER() OVER (
                           PARTITION BY ti.candidate_id ORDER BY ti.snapshot_id DESC
                       ) AS recency_rank
                FROM snapshot_target_intelligence ti
            )
            SELECT c.id
            FROM candidates c
            LEFT JOIN latest_target ti
              ON ti.candidate_id=c.id AND ti.recency_rank=1
            WHERE lower(c.hostname)=? OR lower(COALESCE(ti.canonical_domain, ''))=?
            ORDER BY c.id DESC
            """,
            (normalized_company, normalized_company),
        ).fetchall()
        if not rows:
            return None
        representative = int(rows[0]["id"])
        resolved = self._candidate_company_keys([representative]).get(representative)
        if resolved != normalized_company:
            return None
        return representative, normalized_company

    def _market_document_row(
        self,
        source_id: str,
        document: dict[str, Any],
        *,
        default_policy_version: str,
        default_observed_at: str,
        recorded_at: str,
    ) -> tuple[sqlite3.Row, bool]:
        prohibited_body_keys = (
            "article_body",
            "body",
            "full_text",
            "html",
            "raw_body",
            "raw_html",
        )
        if any(document.get(key) not in (None, "", [], {}) for key in prohibited_body_keys):
            raise ValueError(
                "market documents may retain metadata and summaries, not article bodies"
            )
        metadata = document.get("metadata", document.get("payload", {})) or {}
        if not isinstance(metadata, dict):
            raise ValueError("document metadata must be a JSON object")
        metadata_json = self._market_json(
            metadata,
            field="document metadata",
            maximum_bytes=64_000,
        )
        canonical_url = self._market_text(
            document.get("canonical_url", document.get("url", "")),
            field="canonical_url",
            maximum=2_048,
        )
        external_id = ""
        for key in ("external_id", "document_id", "external_key", "id"):
            if document.get(key) not in (None, ""):
                external_id = self._market_text(
                    document[key],
                    field="source_external_id",
                    maximum=512,
                    required=True,
                )
                break
        if not external_id:
            external_id = canonical_url
        if not external_id:
            raise ValueError("each market document needs an external id or canonical URL")
        canonical_key = self._market_text(
            document.get("canonical_key") or document.get("external_key") or external_id,
            field="canonical_key",
            maximum=512,
            required=True,
        )
        content_hash = self._market_text(
            document.get("content_hash", ""),
            field="content_hash",
            maximum=128,
        )
        if not content_hash:
            content_hash = self._market_document_fingerprint(
                document,
                canonical_url=canonical_url,
                metadata_json=metadata_json,
            )
        revision_key = self._market_text(
            document.get("revision_key", ""),
            field="revision_key",
            maximum=512,
        )
        if not revision_key:
            revision_key = hashlib.sha256(
                f"{canonical_key}\0{external_id}\0{content_hash}".encode()
            ).hexdigest()

        existing = self.connection.execute(
            """
            SELECT * FROM market_documents
            WHERE source_id=? AND revision_key=?
            """,
            (source_id, revision_key),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["source_external_id"]) != external_id
                or str(existing["content_hash"]) != content_hash
                or str(existing["canonical_key"]) != canonical_key
            ):
                raise ValueError("revision_key collides with different document content")
            return existing, False
        existing = self.connection.execute(
            """
            SELECT * FROM market_documents
            WHERE source_id=? AND source_external_id=? AND content_hash=?
            """,
            (source_id, external_id, content_hash),
        ).fetchone()
        if existing is not None:
            return existing, False

        previous = self.connection.execute(
            """
            SELECT id, revision FROM market_documents
            WHERE source_id=? AND canonical_key=?
            ORDER BY revision DESC, id DESC LIMIT 1
            """,
            (source_id, canonical_key),
        ).fetchone()
        revision = int(previous["revision"]) + 1 if previous is not None else 1
        observed_at = self._market_iso_timestamp(
            document.get("observed_at", document.get("fetched_at")),
            field="document observed_at",
            default=default_observed_at,
        )
        published_at = self._market_iso_timestamp(
            document.get("published_at"),
            field="document published_at",
        )
        source_updated_at = self._market_iso_timestamp(
            document.get("source_updated_at", document.get("updated_at")),
            field="document source_updated_at",
        )
        policy_version = self._market_text(
            document.get("source_policy_version") or default_policy_version,
            field="source_policy_version",
            maximum=128,
        )
        self.connection.execute(
            """
            INSERT INTO market_documents(
                source_id, source_external_id, canonical_key, revision_key,
                canonical_url, document_type, title, summary, published_at,
                source_updated_at, observed_at, content_hash, revision,
                supersedes_document_id, causal_root_key, source_policy_version,
                metadata_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                external_id,
                canonical_key,
                revision_key,
                canonical_url,
                self._market_text(
                    document.get("document_type", document.get("kind", "")),
                    field="document_type",
                    maximum=128,
                ),
                self._market_text(
                    document.get("title", ""), field="document title", maximum=1_000
                ),
                self._market_text(
                    document.get("summary", document.get("excerpt", "")),
                    field="document summary",
                    maximum=4_000,
                ),
                published_at,
                source_updated_at,
                observed_at,
                content_hash,
                revision,
                int(previous["id"]) if previous is not None else None,
                self._market_text(
                    document.get("causal_root_key", ""),
                    field="document causal_root_key",
                    maximum=512,
                ),
                policy_version,
                metadata_json,
                recorded_at,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM market_documents WHERE source_id=? AND revision_key=?",
            (source_id, revision_key),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert
            raise RuntimeError("market document insert could not be read back")
        return row, True

    def _market_mention_row(
        self,
        document_row: sqlite3.Row,
        mention: dict[str, Any],
        *,
        recorded_at: str,
    ) -> tuple[sqlite3.Row, bool]:
        document_id = int(document_row["id"])
        raw_name = self._market_text(
            mention.get("raw_name", mention.get("name", "")),
            field="mention raw_name",
            maximum=500,
        )
        identifiers = mention.get("identifiers", mention.get("source_identifiers", {})) or {}
        if not isinstance(identifiers, (dict, list)):
            raise ValueError("mention identifiers must be a JSON object or list")
        identifiers_json = self._market_json(
            identifiers,
            field="mention identifiers",
            maximum_bytes=16_000,
        )
        entity_role = self._market_text(
            mention.get("entity_role", mention.get("role", "unknown")) or "unknown",
            field="mention entity_role",
            maximum=100,
        ).lower()
        exact_subject_role = entity_role in MARKET_EXACT_SUBJECT_ROLES
        normalized_name = self._normalize_market_entity_name(
            mention.get("normalized_name") or raw_name
        )
        candidate_domain = self._normalize_identity_host(
            self._market_text(
                mention.get("candidate_domain", ""),
                field="mention candidate_domain",
                maximum=253,
            )
        ) or ""
        mention_key = self._market_text(
            mention.get("mention_key", ""),
            field="mention_key",
            maximum=512,
        )
        if not mention_key:
            if not raw_name and identifiers_json in {"{}", "[]"} and not candidate_domain:
                raise ValueError(
                    "each market mention needs a name, identifier, or candidate domain"
                )
            mention_key = hashlib.sha256(
                f"{entity_role}\0{normalized_name}\0{identifiers_json}\0{candidate_domain}".encode()
            ).hexdigest()

        existing = self.connection.execute(
            """
            SELECT * FROM market_entity_mentions
            WHERE document_id=? AND mention_key=?
            """,
            (document_id, mention_key),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["normalized_name"]) != normalized_name
                or str(existing["entity_role"]) != entity_role
                or str(existing["identifiers_json"]) != identifiers_json
            ):
                raise ValueError("mention_key collides with a different extracted entity")
            return existing, False

        previous = self.connection.execute(
            """
            SELECT mention.*
            FROM market_entity_mentions mention
            JOIN market_documents document ON document.id=mention.document_id
            WHERE document.source_id=? AND document.canonical_key=?
              AND document.id<>? AND mention.mention_key=?
            ORDER BY document.revision DESC, mention.revision DESC, mention.id DESC
            LIMIT 1
            """,
            (
                document_row["source_id"],
                document_row["canonical_key"],
                document_id,
                mention_key,
            ),
        ).fetchone()
        revision = int(previous["revision"]) + 1 if previous is not None else 1

        identity = None
        requested_candidate = mention.get("candidate_id")
        requested_company = mention.get("company_key")
        if exact_subject_role and (requested_candidate is not None or requested_company):
            identity = self._market_identity_for_candidate(
                int(requested_candidate) if requested_candidate is not None else None,
                str(requested_company) if requested_company else None,
            )
        if exact_subject_role and identity is None and candidate_domain:
            identity = self._market_identity_for_candidate(None, candidate_domain)

        requested_status = str(
            mention.get("resolution_status", mention.get("resolution_state", "unresolved"))
            or "unresolved"
        ).strip().lower()
        if requested_status not in {"unresolved", "ambiguous", "resolved"}:
            requested_status = "unresolved"
        resolution_status = "resolved" if identity is not None else requested_status
        if resolution_status == "resolved" and identity is None:
            resolution_status = "unresolved"
        candidate_id: int | None = identity[0] if identity is not None else None
        company_key: str | None = identity[1] if identity is not None else None
        resolution_method = ""
        resolution_confidence = "low"
        resolution_notes = ""
        resolved_at = None
        if identity is not None:
            resolution_method = "exact_domain_v1" if candidate_domain else "provided_identity_v1"
            resolution_confidence = self._market_text(
                mention.get("resolution_confidence", "high") or "high",
                field="mention resolution_confidence",
                maximum=32,
            )
            resolved_at = recorded_at
        elif resolution_status == "ambiguous":
            resolution_method = self._market_text(
                mention.get("resolution_method", "parser_ambiguous_v1") or "parser_ambiguous_v1",
                field="mention resolution_method",
                maximum=128,
            )
            resolution_notes = self._market_text(
                mention.get("resolution_notes", ""),
                field="mention resolution_notes",
                maximum=1_000,
            )
        elif (
            exact_subject_role
            and previous is not None
            and str(previous["normalized_name"]) == normalized_name
            and str(previous["identifiers_json"]) == identifiers_json
            and str(previous["resolution_status"]) == "resolved"
            and previous["company_key"]
        ):
            previous_identity = self._market_identity_for_candidate(
                int(previous["candidate_id"]) if previous["candidate_id"] is not None else None,
                str(previous["company_key"]),
            )
            if previous_identity is not None:
                resolution_status = "resolved"
                candidate_id, company_key = previous_identity
                resolution_method = "superseded_revision_v1"
                resolution_confidence = str(previous["resolution_confidence"] or "high")
                resolution_notes = "Carried from an identical entity in the prior document revision"
                resolved_at = recorded_at

        metadata = mention.get("metadata", mention.get("payload", {})) or {}
        if not isinstance(metadata, dict):
            raise ValueError("mention metadata must be a JSON object")
        self.connection.execute(
            """
            INSERT INTO market_entity_mentions(
                document_id, mention_key, raw_name, normalized_name, entity_role,
                context_excerpt, identifiers_json, candidate_domain, promotable,
                confidence, resolution_status, resolution_method,
                resolution_confidence, candidate_id, company_key, resolution_notes,
                resolved_at, revision, supersedes_mention_id, metadata_json,
                recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                mention_key,
                raw_name,
                normalized_name,
                entity_role,
                self._market_text(
                    mention.get("context_excerpt", mention.get("context", "")),
                    field="mention context_excerpt",
                    maximum=2_000,
                ),
                identifiers_json,
                candidate_domain,
                int(bool(mention.get("promotable", False))),
                self._market_text(
                    mention.get("confidence", "low") or "low",
                    field="mention confidence",
                    maximum=32,
                ),
                resolution_status,
                resolution_method,
                resolution_confidence,
                candidate_id,
                company_key,
                resolution_notes,
                resolved_at,
                revision,
                int(previous["id"]) if previous is not None else None,
                self._market_json(
                    metadata,
                    field="mention metadata",
                    maximum_bytes=32_000,
                ),
                recorded_at,
                recorded_at,
            ),
        )
        row = self.connection.execute(
            """
            SELECT * FROM market_entity_mentions
            WHERE document_id=? AND mention_key=?
            """,
            (document_id, mention_key),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert
            raise RuntimeError("market mention insert could not be read back")
        return row, True

    def _market_event_row(
        self,
        document_row: sqlite3.Row,
        event: dict[str, Any],
        *,
        mention_rows: dict[str, sqlite3.Row],
        recorded_at: str,
    ) -> tuple[sqlite3.Row, bool]:
        document_id = int(document_row["id"])
        event_type = self._market_text(
            event.get("event_type", event.get("type", "")),
            field="event_type",
            maximum=128,
            required=True,
        )
        effective_at = self._market_iso_timestamp(
            event.get("effective_at", event.get("occurred_at")),
            field="event effective_at",
        )
        amount_json = self._market_json(
            event.get("amount"),
            field="event amount",
            maximum_bytes=4_000,
        )
        causal_root_key = self._market_text(
            event.get("causal_root_key") or document_row["causal_root_key"] or "",
            field="event causal_root_key",
            maximum=512,
        )
        event_key = self._market_text(
            event.get("event_key", event.get("external_id", "")),
            field="event_key",
            maximum=512,
        )
        if not event_key:
            event_key = hashlib.sha256(
                (
                    f"{event_type}\0{effective_at or ''}\0{amount_json}\0{causal_root_key}\0"
                    + str(event.get("summary", ""))
                ).encode()
            ).hexdigest()

        mention_id = None
        mention_key = str(event.get("mention_key") or "").strip()
        if mention_key:
            mention_row = mention_rows.get(mention_key)
            if mention_row is None:
                raise ValueError("event mention_key does not identify a mention in its document")
            mention_id = int(mention_row["id"])
        elif event.get("mention_index") is not None:
            try:
                mention_index = int(event["mention_index"])
                mention_row = list(mention_rows.values())[mention_index]
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError(
                    "event mention_index is outside the document mention list"
                ) from exc
            mention_id = int(mention_row["id"])

        existing = self.connection.execute(
            "SELECT * FROM market_events WHERE document_id=? AND event_key=?",
            (document_id, event_key),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["event_type"]) != event_type
                or str(existing["amount_json"]) != amount_json
                or str(existing["causal_root_key"]) != causal_root_key
                or existing["mention_id"] != mention_id
            ):
                raise ValueError("event_key collides with a different extracted event")
            return existing, False

        prior_params: list[Any] = [
            document_row["source_id"],
            document_row["canonical_key"],
            document_id,
            event_key,
        ]
        causal_match = ""
        if causal_root_key:
            causal_match = " OR (event.causal_root_key=? AND event.event_type=?)"
            prior_params.extend((causal_root_key, event_type))
        previous = self.connection.execute(
            f"""
            SELECT event.*
            FROM market_events event
            JOIN market_documents document ON document.id=event.document_id
            WHERE document.source_id=? AND document.canonical_key=?
              AND document.id<>? AND (event.event_key=?{causal_match})
            ORDER BY document.revision DESC, event.revision DESC, event.id DESC
            LIMIT 1
            """,
            prior_params,
        ).fetchone()
        revision = int(previous["revision"]) + 1 if previous is not None else 1

        direct_identity = None
        has_direct_identity = event.get("candidate_id") is not None or event.get("company_key")
        if mention_id is None and has_direct_identity:
            direct_identity = self._market_identity_for_candidate(
                int(event["candidate_id"]) if event.get("candidate_id") is not None else None,
                str(event.get("company_key") or "") or None,
            )
            if direct_identity is None:
                raise ValueError("event direct identity is not a persisted canonical company")
        metadata = event.get("metadata", event.get("payload", {})) or {}
        if not isinstance(metadata, dict):
            raise ValueError("event metadata must be a JSON object")
        corroboration = event.get("corroboration", event.get("corroboration_evidence", {})) or {}
        self.connection.execute(
            """
            INSERT INTO market_events(
                document_id, mention_id, event_key, event_type, assertion_mode,
                subject_role, effective_at, announced_at, amount_json,
                amount_currency, amount_semantics, causal_root_key,
                independence_cluster, corroboration_json, confidence, summary,
                direct_candidate_id, direct_company_key, semantic_eligible,
                revision, supersedes_event_id, metadata_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                mention_id,
                event_key,
                event_type,
                self._market_text(
                    event.get("assertion_mode", "reported") or "reported",
                    field="event assertion_mode",
                    maximum=64,
                ),
                self._market_text(
                    event.get("subject_role", "subject") or "subject",
                    field="event subject_role",
                    maximum=100,
                ),
                effective_at,
                self._market_iso_timestamp(
                    event.get("announced_at", document_row["published_at"]),
                    field="event announced_at",
                ),
                amount_json,
                self._market_text(
                    event.get("amount_currency", ""),
                    field="event amount_currency",
                    maximum=16,
                ),
                self._market_text(
                    event.get("amount_semantics", ""),
                    field="event amount_semantics",
                    maximum=128,
                ),
                causal_root_key,
                self._market_text(
                    event.get("independence_cluster", ""),
                    field="event independence_cluster",
                    maximum=512,
                ),
                self._market_json(
                    corroboration,
                    field="event corroboration",
                    maximum_bytes=16_000,
                ),
                self._market_text(
                    event.get("confidence", "low") or "low",
                    field="event confidence",
                    maximum=32,
                ),
                self._market_text(
                    event.get("summary", ""), field="event summary", maximum=2_000
                ),
                direct_identity[0] if direct_identity is not None else None,
                direct_identity[1] if direct_identity is not None else None,
                int(bool(event.get("semantic_eligible", event.get("score_eligible", False)))),
                revision,
                int(previous["id"]) if previous is not None else None,
                self._market_json(
                    metadata,
                    field="event metadata",
                    maximum_bytes=32_000,
                ),
                recorded_at,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM market_events WHERE document_id=? AND event_key=?",
            (document_id, event_key),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert
            raise RuntimeError("market event insert could not be read back")
        return row, True

    def _market_candidate_name_index(
        self,
    ) -> dict[str, dict[str, tuple[int, tuple[int, int, int]]]]:
        from .market_identity import group_names

        return group_names(self._market_candidate_name_entries())

    def _market_candidate_name_entries(self, candidate_ids: list[int] | None = None):
        query = """
            SELECT c.id AS candidate_id, c.hostname, c.label_hint,
                   s.id AS snapshot_id, s.observation_json, s.ai_json,
                   ti.canonical_domain, account.company AS account_company
            FROM candidates c
            LEFT JOIN snapshots s ON s.id=(
                SELECT latest.id FROM snapshots latest WHERE latest.candidate_id=c.id
                ORDER BY latest.observed_at DESC, latest.id DESC LIMIT 1
            )
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            LEFT JOIN sales_accounts account ON account.candidate_id=c.id
        """
        if candidate_ids is None:
            rows = self.connection.execute(query)
        else:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = self.connection.execute(
                query + f" WHERE c.id IN ({placeholders})", candidate_ids,
            )
        ignored = {"home", "official website", "unknown", "untitled", "welcome"}
        for row in rows:
            candidate_id = int(row["candidate_id"])
            observation: dict[str, Any] = {}
            ai: dict[str, Any] = {}
            if row["observation_json"]:
                try:
                    observation = json.loads(row["observation_json"])
                except (json.JSONDecodeError, TypeError):
                    observation = {}
            if row["ai_json"]:
                try:
                    ai = json.loads(row["ai_json"])
                except (json.JSONDecodeError, TypeError):
                    ai = {}
            company_key = self._normalize_identity_host(row["canonical_domain"])
            if not company_key:
                company_key = self._normalize_identity_host(observation.get("canonical_host"))
            if not company_key:
                company_key = self._normalize_identity_host(row["hostname"])
            if not company_key:
                continue
            evidence: list[tuple[object, int]] = [
                (row["account_company"], 5),
                (ai.get("company_name"), 4),
                (row["label_hint"], 3),
            ]
            title = str(observation.get("title") or "").strip()
            if title:
                evidence.append(
                    (
                        concise_company_name(
                            ai_name="",
                            title=title,
                            label_hint=str(row["label_hint"] or ""),
                            canonical_domain=company_key,
                            hostname=str(row["hostname"]),
                        ),
                        2,
                    )
                )
            discovery = (
                observation.get("web", {})
                .get("frontier", {})
                .get("features", {})
                .get("frontier_discovery", {})
            )
            if isinstance(discovery, dict):
                for key, rank in (
                    ("first_party_legal_names", 6),
                    ("legal_names", 6),
                    ("first_party_company_names", 5),
                    ("company_names", 5),
                ):
                    names = discovery.get(key, [])
                    if isinstance(names, list):
                        evidence.extend((name, rank) for name in names[:10])
            for raw_name, evidence_rank in evidence:
                normalized_name = self._normalize_market_entity_name(raw_name)
                if len(normalized_name) < 2 or normalized_name in ignored:
                    continue
                rank = (int(row["snapshot_id"] or 0), evidence_rank, candidate_id)
                yield normalized_name, company_key, candidate_id, rank

    def _update_market_mention_resolution_uncommitted(
        self,
        mention_id: int,
        *,
        resolution_status: str,
        candidate_id: int | None,
        company_key: str | None,
        resolution_method: str,
        resolution_confidence: str,
        resolution_notes: str,
        resolved_at: str | None,
    ) -> sqlite3.Row:
        if resolution_status not in {"unresolved", "ambiguous", "resolved"}:
            raise ValueError("resolution_status must be unresolved, ambiguous, or resolved")
        if resolution_status == "resolved":
            identity = self._market_identity_for_candidate(candidate_id, company_key)
            if identity is None:
                raise ValueError("resolved mentions require a persisted canonical company")
            candidate_id, company_key = identity
            resolved_at = resolved_at or _now()
        else:
            candidate_id = None
            company_key = None
            resolved_at = None
        cursor = self.connection.execute(
            """
            UPDATE market_entity_mentions
            SET resolution_status=?, resolution_method=?, resolution_confidence=?,
                candidate_id=?, company_key=?, resolution_notes=?, resolved_at=?,
                updated_at=?
            WHERE id=?
            """,
            (
                resolution_status,
                resolution_method[:128],
                resolution_confidence[:32],
                candidate_id,
                company_key,
                resolution_notes[:1_000],
                resolved_at,
                _now(),
                int(mention_id),
            ),
        )
        if not cursor.rowcount:
            raise KeyError(f"Unknown market mention {mention_id}")
        row = self.connection.execute(
            "SELECT * FROM market_entity_mentions WHERE id=?", (int(mention_id),)
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the update
            raise RuntimeError("updated market mention could not be read back")
        return row

    def _resolve_market_mentions_exact_uncommitted(
        self,
        mention_ids: list[int] | None = None,
        *,
        names: dict,
    ) -> dict[str, Any]:
        conditions = ["resolution_status='unresolved'", "normalized_name<>''"]
        params: list[Any] = []
        if mention_ids is not None:
            identifiers = list(dict.fromkeys(int(value) for value in mention_ids))
            if not identifiers:
                return {
                    "resolved": 0,
                    "ambiguous": 0,
                    "unresolved": 0,
                    "resolved_mention_ids": [],
                    "resolved_company_keys": [],
                    "ambiguous_mention_ids": [],
                }
            placeholders = ", ".join("?" for _ in identifiers)
            conditions.append(f"id IN ({placeholders})")
            params.extend(identifiers)
        rows = self.connection.execute(
            f"""
            SELECT id, raw_name, normalized_name, entity_role
            FROM market_entity_mentions
            WHERE {" AND ".join(conditions)}
            ORDER BY id
            """,
            params,
        ).fetchall()
        result: dict[str, Any] = {
            "resolved": 0,
            "ambiguous": 0,
            "unresolved": 0,
            "resolved_mention_ids": [],
            "resolved_company_keys": [],
            "ambiguous_mention_ids": [],
        }
        for row in rows:
            if str(row["entity_role"]).strip().lower() not in MARKET_EXACT_SUBJECT_ROLES:
                result["unresolved"] += 1
                continue
            normalized_name = self._normalize_market_entity_name(
                row["raw_name"] or row["normalized_name"]
            )
            companies = names.get(normalized_name, {})
            if len(companies) == 1:
                company_key, (candidate_id, _) = next(iter(companies.items()))
                self._update_market_mention_resolution_uncommitted(
                    int(row["id"]),
                    resolution_status="resolved",
                    candidate_id=candidate_id,
                    company_key=company_key,
                    resolution_method="exact_name_v1",
                    resolution_confidence="high",
                    resolution_notes="Matched exactly one canonical company in latest evidence",
                    resolved_at=_now(),
                )
                result["resolved"] += 1
                result["resolved_mention_ids"].append(int(row["id"]))
                result["resolved_company_keys"].append(company_key)
            elif len(companies) > 1:
                self._update_market_mention_resolution_uncommitted(
                    int(row["id"]),
                    resolution_status="ambiguous",
                    candidate_id=None,
                    company_key=None,
                    resolution_method="exact_name_collision_v1",
                    resolution_confidence="low",
                    resolution_notes=(
                        f"Exact name appears on {len(companies)} canonical companies; quarantined"
                    ),
                    resolved_at=None,
                )
                result["ambiguous"] += 1
                result["ambiguous_mention_ids"].append(int(row["id"]))
            else:
                result["unresolved"] += 1
        result["resolved_company_keys"] = sorted(set(result["resolved_company_keys"]))
        return result

    def _market_company_batch_refs(
        self,
        *,
        document_ids: list[int],
        mention_ids: list[int],
        event_ids: list[int],
    ) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, set[Any]]] = {}
        resolved_mention_ids: list[int] = []

        def group(company_key: str) -> dict[str, set[Any]]:
            return groups.setdefault(
                company_key,
                {
                    "candidate_ids": set(),
                    "document_ids": set(),
                    "source_external_ids": set(),
                    "mention_ids": set(),
                    "mention_keys": set(),
                    "event_ids": set(),
                    "event_keys": set(),
                },
            )

        mention_conditions: list[str] = []
        mention_params: list[Any] = []
        if document_ids:
            placeholders = ", ".join("?" for _ in document_ids)
            mention_conditions.append(f"mention.document_id IN ({placeholders})")
            mention_params.extend(document_ids)
        if mention_ids:
            placeholders = ", ".join("?" for _ in mention_ids)
            mention_conditions.append(f"mention.id IN ({placeholders})")
            mention_params.extend(mention_ids)
        if mention_conditions:
            rows = self.connection.execute(
                f"""
                SELECT mention.id, mention.document_id, mention.candidate_id,
                       mention.company_key, mention.mention_key,
                       document.source_external_id
                FROM market_entity_mentions mention
                JOIN market_documents document ON document.id=mention.document_id
                WHERE mention.resolution_status='resolved'
                  AND ({" OR ".join(mention_conditions)})
                """,
                mention_params,
            ).fetchall()
            for row in rows:
                company = str(row["company_key"])
                bucket = group(company)
                resolved_mention_ids.append(int(row["id"]))
                bucket["document_ids"].add(int(row["document_id"]))
                bucket["source_external_ids"].add(str(row["source_external_id"]))
                bucket["mention_ids"].add(int(row["id"]))
                bucket["mention_keys"].add(str(row["mention_key"]))
                if row["candidate_id"] is not None:
                    bucket["candidate_ids"].add(int(row["candidate_id"]))

        # A document/event may have been persisted while its subject was unresolved. If an
        # exact replay later resolves that subject, surface the already-stored event now so the
        # first safe company projection is not lost merely because the event row was reused.
        if resolved_mention_ids:
            placeholders = ", ".join("?" for _ in resolved_mention_ids)
            rows = self.connection.execute(
                f"""
                SELECT event.id, event.document_id, event.event_key,
                       document.source_external_id, mention.candidate_id,
                       mention.company_key
                FROM market_events event
                JOIN market_entity_mentions mention ON mention.id=event.mention_id
                JOIN market_documents document ON document.id=event.document_id
                WHERE mention.id IN ({placeholders})
                  AND mention.resolution_status='resolved'
                """,
                resolved_mention_ids,
            ).fetchall()
            for row in rows:
                company = str(row["company_key"])
                bucket = group(company)
                bucket["document_ids"].add(int(row["document_id"]))
                bucket["source_external_ids"].add(str(row["source_external_id"]))
                bucket["event_ids"].add(int(row["id"]))
                bucket["event_keys"].add(str(row["event_key"]))
                if row["candidate_id"] is not None:
                    bucket["candidate_ids"].add(int(row["candidate_id"]))

        if event_ids:
            placeholders = ", ".join("?" for _ in event_ids)
            rows = self.connection.execute(
                f"""
                SELECT event.id, event.document_id, event.event_key,
                       document.source_external_id,
                       COALESCE(mention.candidate_id, event.direct_candidate_id)
                           AS candidate_id,
                       COALESCE(mention.company_key, event.direct_company_key)
                           AS company_key
                FROM market_events event
                JOIN market_documents document ON document.id=event.document_id
                LEFT JOIN market_entity_mentions mention ON mention.id=event.mention_id
                WHERE event.id IN ({placeholders})
                  AND COALESCE(mention.company_key, event.direct_company_key) IS NOT NULL
                  AND (mention.id IS NULL OR mention.resolution_status='resolved')
                """,
                event_ids,
            ).fetchall()
            for row in rows:
                company = str(row["company_key"])
                bucket = group(company)
                bucket["document_ids"].add(int(row["document_id"]))
                bucket["source_external_ids"].add(str(row["source_external_id"]))
                bucket["event_ids"].add(int(row["id"]))
                bucket["event_keys"].add(str(row["event_key"]))
                if row["candidate_id"] is not None:
                    bucket["candidate_ids"].add(int(row["candidate_id"]))

        return [
            {
                "company_key": company_key,
                **{key: sorted(values) for key, values in refs.items()},
            }
            for company_key, refs in sorted(groups.items())
        ]

    def persist_market_batch(
        self,
        source_or_batch: str | dict[str, Any],
        batch: dict[str, Any] | list[dict[str, Any]] | str | None = None,
        *,
        cursor: object | None = None,
        polled_at: datetime | date | str | None = None,
        resolve_exact_names: bool = True,
    ) -> dict[str, Any]:
        """Persist one parsed source batch and advance its cursor in the same transaction.

        Callers may pass a parser batch directly (it must contain ``source_id``), or pass
        ``source_id`` and a document list/object separately. Exact payload replays reuse rows;
        changed payloads append revisions and retain supersession lineage.
        """
        batch_payload: dict[str, Any]
        if batch is None and not isinstance(source_or_batch, str):
            batch_payload = self._market_mapping(source_or_batch, field="market batch")
            raw_source_id = batch_payload.get("source_id")
        elif (
            batch is None
            and isinstance(source_or_batch, str)
            and source_or_batch.lstrip().startswith(("{", "["))
        ):
            decoded = json.loads(source_or_batch)
            batch_payload = self._market_mapping(decoded, field="market batch")
            raw_source_id = batch_payload.get("source_id")
        else:
            raw_source_id = source_or_batch
            if isinstance(batch, str):
                try:
                    batch = json.loads(batch)
                except json.JSONDecodeError as exc:
                    raise ValueError("market batch must be valid JSON") from exc
            if isinstance(batch, list):
                batch_payload = {"documents": batch}
            elif isinstance(batch, dict):
                batch_payload = dict(batch)
            else:
                raise ValueError("market batch must be a JSON object or document list")
            embedded_source = batch_payload.get("source_id")
            if embedded_source and str(embedded_source).strip() != str(raw_source_id).strip():
                raise ValueError("batch source_id does not match the requested source")
        source_id = self._market_text(
            raw_source_id,
            field="source_id",
            maximum=128,
            required=True,
        ).lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", source_id):
            raise ValueError("source_id must be a stable lowercase scanner identifier")
        documents = batch_payload.get("documents", [])
        if not isinstance(documents, list):
            raise ValueError("market batch documents must be a list")
        if len(documents) > 10_000:
            raise ValueError("market batch exceeds the 10,000-document persistence cap")
        parsed_documents = [
            self._market_mapping(document, field="market document") for document in documents
        ]
        poll_timestamp = self._market_iso_timestamp(
            polled_at or batch_payload.get("polled_at"),
            field="market batch polled_at",
            default=_now(),
        )
        if poll_timestamp is None:  # pragma: no cover - default is always present
            raise RuntimeError("market batch poll timestamp is unavailable")
        recorded_at = _now()
        policy_version = self._market_text(
            batch_payload.get("source_policy_version", ""),
            field="source_policy_version",
            maximum=128,
        )
        cursor_supplied = cursor is not None or "cursor" in batch_payload
        cursor_value = cursor if cursor is not None else batch_payload.get("cursor")
        etag_supplied = "etag" in batch_payload
        last_modified_supplied = "last_modified" in batch_payload

        inserted_documents = 0
        inserted_mentions = 0
        inserted_events = 0
        reused_documents = 0
        inserted_document_ids: list[int] = []
        inserted_mention_ids: list[int] = []
        inserted_event_ids: list[int] = []
        mention_ids: list[int] = []
        from .market_identity import write_with_identity

        has_mentions = resolve_exact_names and any(doc.get("mentions") for doc in parsed_documents)
        with write_with_identity(self, resolve=has_mentions) as names:
            for document in parsed_documents:
                document_row, inserted = self._market_document_row(
                    source_id,
                    document,
                    default_policy_version=policy_version,
                    default_observed_at=poll_timestamp,
                    recorded_at=recorded_at,
                )
                inserted_documents += int(inserted)
                reused_documents += int(not inserted)
                if inserted:
                    inserted_document_ids.append(int(document_row["id"]))
                raw_mentions = document.get("mentions", [])
                raw_events = document.get("events", [])
                if not isinstance(raw_mentions, list) or len(raw_mentions) > 1_000:
                    raise ValueError("document mentions must be a list of at most 1,000 items")
                if not isinstance(raw_events, list) or len(raw_events) > 1_000:
                    raise ValueError("document events must be a list of at most 1,000 items")
                mention_rows: dict[str, sqlite3.Row] = {}
                for raw_mention in raw_mentions:
                    mention = self._market_mapping(raw_mention, field="market mention")
                    mention_row, mention_inserted = self._market_mention_row(
                        document_row,
                        mention,
                        recorded_at=recorded_at,
                    )
                    key = str(mention_row["mention_key"])
                    if key in mention_rows:
                        raise ValueError("document contains duplicate mention keys")
                    mention_rows[key] = mention_row
                    mention_ids.append(int(mention_row["id"]))
                    inserted_mentions += int(mention_inserted)
                    if mention_inserted:
                        inserted_mention_ids.append(int(mention_row["id"]))
                seen_event_keys: set[str] = set()
                for raw_event in raw_events:
                    event = self._market_mapping(raw_event, field="market event")
                    event_row, event_inserted = self._market_event_row(
                        document_row,
                        event,
                        mention_rows=mention_rows,
                        recorded_at=recorded_at,
                    )
                    event_key = str(event_row["event_key"])
                    if event_key in seen_event_keys:
                        raise ValueError("document contains duplicate event keys")
                    seen_event_keys.add(event_key)
                    inserted_events += int(event_inserted)
                    if event_inserted:
                        inserted_event_ids.append(int(event_row["id"]))

            resolution = (
                self._resolve_market_mentions_exact_uncommitted(mention_ids, names=names)
                if resolve_exact_names
                else {
                    "resolved": 0,
                    "ambiguous": 0,
                    "unresolved": len(set(mention_ids)),
                    "resolved_mention_ids": [],
                    "resolved_company_keys": [],
                    "ambiguous_mention_ids": [],
                }
            )
            company_batches = self._market_company_batch_refs(
                document_ids=inserted_document_ids,
                mention_ids=[
                    *inserted_mention_ids,
                    *resolution["resolved_mention_ids"],
                ],
                event_ids=inserted_event_ids,
            )
            current_cursor = self.connection.execute(
                "SELECT * FROM market_source_cursors WHERE source_id=?", (source_id,)
            ).fetchone()
            if cursor_supplied:
                cursor_json = self._market_json(
                    cursor_value,
                    field="market source cursor",
                    maximum_bytes=32_000,
                )
            elif current_cursor is not None:
                cursor_json = str(current_cursor["cursor_json"])
            else:
                cursor_json = "{}"
            etag = (
                self._market_text(
                    batch_payload.get("etag", ""), field="market source etag", maximum=1_000
                )
                if etag_supplied
                else str(current_cursor["etag"] if current_cursor is not None else "")
            )
            last_modified = (
                self._market_text(
                    batch_payload.get("last_modified", ""),
                    field="market source last_modified",
                    maximum=1_000,
                )
                if last_modified_supplied
                else str(current_cursor["last_modified"] if current_cursor is not None else "")
            )
            aggregate = self.connection.execute(
                """
                SELECT COUNT(*) AS document_count,
                       MAX(observed_at) AS latest_document_at,
                       MAX(published_at) AS latest_published_at
                FROM market_documents WHERE source_id=?
                """,
                (source_id,),
            ).fetchone()
            event_count = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM market_events event
                    JOIN market_documents document ON document.id=event.document_id
                    WHERE document.source_id=?
                    """,
                    (source_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                """
                INSERT INTO market_source_cursors(
                    source_id, source_policy_version, cursor_json, etag,
                    last_modified, last_polled_at, last_success_at,
                    latest_document_at, latest_published_at, documents_persisted,
                    events_persisted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_policy_version=CASE
                        WHEN excluded.last_success_at>=market_source_cursors.last_success_at
                             AND excluded.source_policy_version<>''
                        THEN excluded.source_policy_version
                        ELSE market_source_cursors.source_policy_version END,
                    cursor_json=CASE
                        WHEN excluded.last_success_at>=market_source_cursors.last_success_at
                        THEN excluded.cursor_json ELSE market_source_cursors.cursor_json END,
                    etag=CASE
                        WHEN excluded.last_success_at>=market_source_cursors.last_success_at
                        THEN excluded.etag ELSE market_source_cursors.etag END,
                    last_modified=CASE
                        WHEN excluded.last_success_at>=market_source_cursors.last_success_at
                        THEN excluded.last_modified ELSE market_source_cursors.last_modified END,
                    last_polled_at=MAX(
                        market_source_cursors.last_polled_at, excluded.last_polled_at
                    ),
                    last_success_at=MAX(
                        market_source_cursors.last_success_at, excluded.last_success_at
                    ),
                    latest_document_at=CASE
                        WHEN market_source_cursors.latest_document_at IS NULL
                          OR excluded.latest_document_at>market_source_cursors.latest_document_at
                        THEN excluded.latest_document_at
                        ELSE market_source_cursors.latest_document_at END,
                    latest_published_at=CASE
                        WHEN market_source_cursors.latest_published_at IS NULL
                          OR excluded.latest_published_at>market_source_cursors.latest_published_at
                        THEN excluded.latest_published_at
                        ELSE market_source_cursors.latest_published_at END,
                    documents_persisted=excluded.documents_persisted,
                    events_persisted=excluded.events_persisted,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    policy_version,
                    cursor_json,
                    etag,
                    last_modified,
                    poll_timestamp,
                    poll_timestamp,
                    aggregate["latest_document_at"],
                    aggregate["latest_published_at"],
                    int(aggregate["document_count"]),
                    event_count,
                    recorded_at,
                ),
            )
        return {
            "source_id": source_id,
            "documents_inserted": inserted_documents,
            "documents_reused": reused_documents,
            "mentions_inserted": inserted_mentions,
            "events_inserted": inserted_events,
            "resolution": resolution,
            "resolved_company_keys": [
                item["company_key"] for item in company_batches
            ],
            "company_batches": company_batches,
            "cursor": self.get_market_source_cursor(source_id),
        }

    @staticmethod
    def _market_document_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["id"] = int(row["id"])
        value["revision"] = int(row["revision"])
        value["supersedes_document_id"] = (
            int(row["supersedes_document_id"])
            if row["supersedes_document_id"] is not None
            else None
        )
        value["metadata"] = json.loads(row["metadata_json"] or "{}")
        value.pop("metadata_json", None)
        return value

    @staticmethod
    def _market_mention_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("id", "document_id", "revision"):
            value[key] = int(row[key])
        for key in ("candidate_id", "supersedes_mention_id"):
            value[key] = int(row[key]) if row[key] is not None else None
        value["promotable"] = bool(row["promotable"])
        value["identifiers"] = json.loads(row["identifiers_json"] or "{}")
        value["metadata"] = json.loads(row["metadata_json"] or "{}")
        value.pop("identifiers_json", None)
        value.pop("metadata_json", None)
        return value

    @staticmethod
    def _market_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("id", "document_id", "revision"):
            value[key] = int(row[key])
        for key in (
            "mention_id",
            "direct_candidate_id",
            "supersedes_event_id",
            "resolved_candidate_id",
        ):
            value[key] = int(row[key]) if row[key] is not None else None
        value["semantic_eligible"] = bool(row["semantic_eligible"])
        value["score_eligible"] = bool(row["score_eligible"])
        value["amount"] = json.loads(row["amount_json"] or "null")
        value["corroboration"] = json.loads(row["corroboration_json"] or "{}")
        value["metadata"] = json.loads(row["metadata_json"] or "{}")
        value.pop("amount_json", None)
        value.pop("corroboration_json", None)
        value.pop("metadata_json", None)
        return value

    def list_market_documents(
        self,
        *,
        source_id: str | None = None,
        external_id: str | None = None,
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if source_id:
            conditions.append("document.source_id=?")
            params.append(source_id.strip().lower())
        if external_id:
            conditions.append("document.source_external_id=?")
            params.append(external_id.strip())
        if latest_only:
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM market_documents newer
                    WHERE newer.source_id=document.source_id
                      AND newer.canonical_key=document.canonical_key
                      AND newer.revision>document.revision
                )
                """
            )
        params.append(max(1, min(int(limit), 10_000)))
        rows = self.connection.execute(
            f"""
            SELECT document.* FROM market_documents document
            WHERE {" AND ".join(conditions)}
            ORDER BY document.published_at DESC, document.observed_at DESC,
                     document.revision DESC, document.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._market_document_dict(row) for row in rows]

    def list_market_company_documents(
        self,
        company_key: str,
        *,
        latest_only: bool = True,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return only documents safely connected to one resolved canonical company."""
        normalized_company = self._normalized_company_key(company_key)
        latest_clause = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM market_documents newer
                WHERE newer.source_id=document.source_id
                  AND newer.canonical_key=document.canonical_key
                  AND newer.revision>document.revision
            )
            """
            if latest_only
            else ""
        )
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT document.*
            FROM market_documents document
            LEFT JOIN market_entity_mentions mention
              ON mention.document_id=document.id
             AND mention.resolution_status='resolved'
            LEFT JOIN market_events event ON event.document_id=document.id
            LEFT JOIN market_entity_mentions event_mention
              ON event_mention.id=event.mention_id
             AND event_mention.resolution_status='resolved'
            WHERE (
                mention.company_key=?
                OR COALESCE(event_mention.company_key, event.direct_company_key)=?
            )
            {latest_clause}
            ORDER BY document.published_at DESC, document.observed_at DESC,
                     document.revision DESC, document.id DESC
            LIMIT ?
            """,
            (normalized_company, normalized_company, max(1, min(int(limit), 10_000))),
        ).fetchall()
        return [self._market_document_dict(row) for row in rows]

    def list_market_entity_mentions(
        self,
        *,
        source_id: str | None = None,
        document_id: int | None = None,
        resolution_status: str | None = None,
        company_key: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if source_id:
            conditions.append("document.source_id=?")
            params.append(source_id.strip().lower())
        if document_id is not None:
            conditions.append("mention.document_id=?")
            params.append(int(document_id))
        if resolution_status:
            conditions.append("mention.resolution_status=?")
            params.append(resolution_status.strip().lower())
        if company_key:
            conditions.append("mention.company_key=?")
            params.append(self._normalized_company_key(company_key))
        params.append(max(1, min(int(limit), 10_000)))
        rows = self.connection.execute(
            f"""
            SELECT mention.*, document.source_id,
                   document.source_external_id, document.canonical_url
            FROM market_entity_mentions mention
            JOIN market_documents document ON document.id=mention.document_id
            WHERE {" AND ".join(conditions)}
            ORDER BY document.published_at DESC, mention.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._market_mention_dict(row) for row in rows]

    def list_market_mentions(self, **filters: Any) -> list[dict[str, Any]]:
        """Compatibility shorthand for callers that do not need the table's full name."""
        return self.list_market_entity_mentions(**filters)

    def list_market_events(
        self,
        *,
        source_id: str | None = None,
        document_id: int | None = None,
        event_type: str | None = None,
        company_key: str | None = None,
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if source_id:
            conditions.append("document.source_id=?")
            params.append(source_id.strip().lower())
        if document_id is not None:
            conditions.append("event.document_id=?")
            params.append(int(document_id))
        if event_type:
            conditions.append("event.event_type=?")
            params.append(event_type.strip())
        if company_key:
            conditions.append("COALESCE(mention.company_key, event.direct_company_key)=?")
            params.append(self._normalized_company_key(company_key))
        if latest_only:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM market_events newer "
                "WHERE newer.supersedes_event_id=event.id)"
            )
        params.append(max(1, min(int(limit), 10_000)))
        rows = self.connection.execute(
            f"""
            SELECT event.*, document.source_id, document.source_external_id,
                   document.canonical_url, mention.resolution_status,
                   COALESCE(mention.candidate_id, event.direct_candidate_id)
                       AS resolved_candidate_id,
                   COALESCE(mention.company_key, event.direct_company_key)
                       AS resolved_company_key,
                   CASE
                       WHEN event.semantic_eligible=1
                        AND COALESCE(mention.company_key, event.direct_company_key) IS NOT NULL
                        AND (mention.id IS NULL OR mention.resolution_status='resolved')
                       THEN 1 ELSE 0 END AS score_eligible
            FROM market_events event
            JOIN market_documents document ON document.id=event.document_id
            LEFT JOIN market_entity_mentions mention ON mention.id=event.mention_id
            WHERE {" AND ".join(conditions)}
            ORDER BY COALESCE(event.effective_at, document.published_at) DESC, event.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._market_event_dict(row) for row in rows]

    def resolve_market_mention(
        self,
        mention_id: int,
        *,
        resolution_status: str = "resolved",
        candidate_id: int | None = None,
        company_key: str | None = None,
        resolution_method: str = "operator_manual_v1",
        resolution_confidence: str = "high",
        resolution_notes: str = "",
        resolved_at: datetime | date | str | None = None,
    ) -> dict[str, Any]:
        resolved_at_text = self._market_iso_timestamp(
            resolved_at,
            field="mention resolved_at",
            default=_now() if resolution_status == "resolved" else None,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._update_market_mention_resolution_uncommitted(
                int(mention_id),
                resolution_status=resolution_status.strip().lower(),
                candidate_id=candidate_id,
                company_key=company_key,
                resolution_method=resolution_method,
                resolution_confidence=resolution_confidence,
                resolution_notes=resolution_notes,
                resolved_at=resolved_at_text,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._market_mention_dict(row)

    def resolve_market_mentions_exact_names(
        self,
        mention_ids: list[int] | None = None,
    ) -> dict[str, int]:
        from .market_identity import write_with_identity

        with write_with_identity(self, resolve=mention_ids != []) as names:
            return self._resolve_market_mentions_exact_uncommitted(mention_ids, names=names)

    @staticmethod
    def _market_cursor_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["cursor"] = json.loads(row["cursor_json"] or "{}")
        value["documents_persisted"] = int(row["documents_persisted"])
        value["events_persisted"] = int(row["events_persisted"])
        value.pop("cursor_json", None)
        return value

    def get_market_source_cursor(self, source_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM market_source_cursors WHERE source_id=?",
            (source_id.strip().lower(),),
        ).fetchone()
        return self._market_cursor_dict(row) if row is not None else None

    def get_market_source_cursors(
        self,
        source_ids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if source_ids is not None:
            normalized = list(dict.fromkeys(value.strip().lower() for value in source_ids if value))
            if not normalized:
                return {}
            placeholders = ", ".join("?" for _ in normalized)
            conditions.append(f"source_id IN ({placeholders})")
            params.extend(normalized)
        rows = self.connection.execute(
            f"""
            SELECT * FROM market_source_cursors
            WHERE {" AND ".join(conditions)}
            ORDER BY source_id
            """,
            params,
        ).fetchall()
        return {str(row["source_id"]): self._market_cursor_dict(row) for row in rows}

    def market_source_state(
        self,
        source_id: str | None = None,
    ) -> dict[str, Any] | dict[str, dict[str, Any]] | None:
        """Compact cursor/state boundary for feed runners."""
        if source_id is not None:
            return self.get_market_source_cursor(source_id)
        return self.get_market_source_cursors()

    def summarize_market_source_recency(
        self,
        *,
        as_of: datetime | date | str | None = None,
        stale_after_hours: float = 24.0,
    ) -> dict[str, Any]:
        as_of_text = self._market_iso_timestamp(as_of, field="recency as_of", default=_now())
        if as_of_text is None:  # pragma: no cover - default is always present
            raise RuntimeError("recency timestamp is unavailable")
        as_of_time = datetime.fromisoformat(as_of_text)
        sources: list[dict[str, Any]] = []
        for cursor_row in self.get_market_source_cursors().values():
            last_success = datetime.fromisoformat(str(cursor_row["last_success_at"]))
            age_hours = max(0.0, (as_of_time - last_success).total_seconds() / 3_600)
            unresolved = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM market_entity_mentions mention
                    JOIN market_documents document ON document.id=mention.document_id
                    WHERE document.source_id=? AND mention.resolution_status<>'resolved'
                    """,
                    (cursor_row["source_id"],),
                ).fetchone()[0]
            )
            sources.append(
                {
                    **cursor_row,
                    "age_hours": age_hours,
                    "stale": age_hours > max(0.0, float(stale_after_hours)),
                    "unresolved_mentions": unresolved,
                }
            )
        stale_sources = sum(int(source["stale"]) for source in sources)
        return {
            "as_of": as_of_text,
            "stale_after_hours": max(0.0, float(stale_after_hours)),
            "source_count": len(sources),
            "fresh_sources": len(sources) - stale_sources,
            "stale_sources": stale_sources,
            "sources": sources,
        }

    def start_run(self, config: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at, status, config_json) VALUES (?, 'running', ?)",
            (_now(), json.dumps(config, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO run_progress(run_id, updated_at) VALUES (?, ?)", (run_id, _now())
        )
        self.connection.commit()
        return run_id

    def track_run_phases(self, run_id: int) -> None:
        """Opt-in, bounded stage wall times for the CLI-owned run; no evidence cache."""
        self._run_phase_timing = {"run_id": run_id, "phase": "planning",
                                  "since": time.monotonic(), "seconds": {}}

    def _track_run_phase(self, run_id: int, phase: str) -> dict[str, float] | None:
        timing = getattr(self, "_run_phase_timing", None)
        if not timing or timing["run_id"] != run_id:
            return None
        now = time.monotonic()
        seconds = timing["seconds"]
        previous = timing["phase"]
        seconds[previous] = seconds.get(previous, 0.0) + max(0, now - timing["since"])
        timing.update(phase=phase, since=now)
        return {name: round(value, 6) for name, value in seconds.items()}

    def finish_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        timings = self._track_run_phase(run_id, status)
        if timings is not None:
            summary = {**summary, "phase_wall_seconds": timings}
        self.connection.execute(
            "UPDATE runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
            (_now(), status, json.dumps(summary, sort_keys=True), run_id),
        )
        self.connection.execute(
            "UPDATE run_progress SET phase=?, updated_at=? WHERE run_id=?",
            (status, _now(), run_id),
        )
        self.connection.commit()

    def update_run_progress(
        self,
        run_id: int,
        *,
        phase: str,
        discovered: int | None = None,
        inspected: int | None = None,
        enriched: int | None = None,
        external_checked: int | None = None,
        total_candidates: int | None = None,
    ) -> None:
        current = self.connection.execute(
            "SELECT * FROM run_progress WHERE run_id=?", (run_id,)
        ).fetchone()
        if current is None:
            return
        timestamp = _now()
        lease_expires_at = (
            datetime.fromisoformat(timestamp) + timedelta(minutes=COMPANY_SCAN_LEASE_MINUTES)
        ).isoformat()
        self.connection.execute(
            """
            UPDATE run_progress
            SET phase=?, discovered=?, inspected=?, enriched=?, external_checked=?,
                total_candidates=?, updated_at=?
            WHERE run_id=?
            """,
            (
                phase,
                discovered if discovered is not None else current["discovered"],
                inspected if inspected is not None else current["inspected"],
                enriched if enriched is not None else current["enriched"],
                external_checked if external_checked is not None else current["external_checked"],
                total_candidates if total_candidates is not None else current["total_candidates"],
                timestamp,
                run_id,
            ),
        )
        timing = getattr(self, "_run_phase_timing", None)
        tracked = timing is not None and timing["run_id"] == run_id
        tick = time.monotonic()
        renew_lease = not tracked or tick - timing.get("lease_renewed_at", float("-inf")) >= 30
        if renew_lease:
            self.connection.execute(
                """
                UPDATE company_scan_work
                SET lease_expires_at=?, updated_at=?
                WHERE status='running' AND claimed_run_id=?
                """,
                (lease_expires_at, timestamp, run_id),
            )
        self.connection.commit()
        if tracked and renew_lease:
            timing["lease_renewed_at"] = tick
        self._track_run_phase(run_id, phase)

    def record_source_error(self, run_id: int, apex: str, error: str) -> None:
        self.connection.execute(
            "INSERT INTO source_errors(run_id, source_apex, error, created_at) VALUES (?, ?, ?, ?)",
            (run_id, apex, error[:2_000], _now()),
        )
        self.connection.commit()

    @staticmethod
    def _merge_discovery_assertions(
        *groups: list[DiscoveryAssertionEvidence] | list[dict[str, Any]],
    ) -> list[DiscoveryAssertionEvidence]:
        """Dedupe bounded discovery context without turning it into a current fact."""
        merged: dict[tuple[str, str, str, str], DiscoveryAssertionEvidence] = {}
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        evidence_rank = {
            "logo_filename": 0,
            "logo_label": 1,
            "internal_record": 2,
            "anchor": 3,
            "linked_logo": 4,
        }
        for group in groups:
            for raw in group:
                try:
                    assertion = DiscoveryAssertionEvidence.model_validate(raw)
                except (TypeError, ValueError):
                    continue
                key = (
                    assertion.publisher_domain,
                    assertion.relation_type,
                    assertion.source_url.strip(),
                    assertion.context_hash.strip(),
                )
                current = merged.get(key)
                if current is None:
                    merged[key] = assertion
                    continue
                stronger_evidence = (
                    assertion
                    if evidence_rank[assertion.evidence_kind]
                    > evidence_rank[current.evidence_kind]
                    else current
                )
                merged[key] = current.model_copy(
                    update={
                        "context_excerpt": max(
                            (current.context_excerpt, assertion.context_excerpt),
                            key=len,
                        ),
                        "published_at": current.published_at or assertion.published_at,
                        "confidence": (
                            assertion.confidence
                            if confidence_rank[assertion.confidence]
                            > confidence_rank[current.confidence]
                            else current.confidence
                        ),
                        # Discovery context remains non-current unless every retained
                        # public assertion explicitly proves otherwise.
                        "current_relationship_unverified": (
                            current.current_relationship_unverified
                            or assertion.current_relationship_unverified
                        ),
                        "evidence_kind": stronger_evidence.evidence_kind,
                        # Responsive/CDN asset URLs may rotate while the semantic assertion
                        # identity stays stable. Retain the newest non-empty URL so a repeated
                        # scan converges instead of rediscovering the same proof forever.
                        "asset_url": assertion.asset_url or current.asset_url,
                    }
                )
        return list(merged.values())[:30]

    @classmethod
    def _discovery_assertions_json(
        cls,
        assertions: list[DiscoveryAssertionEvidence],
    ) -> str:
        return json.dumps(
            [item.model_dump(mode="json") for item in assertions],
            sort_keys=True,
        )

    def _merge_candidates_for_storage(
        self, candidates: list[DiscoveredHost]
    ) -> list[DiscoveredHost]:
        """Merge repeated discovery without allowing a later path to erase lineage."""

        merged_by_host: dict[str, DiscoveredHost] = {}
        for candidate in candidates:
            current = merged_by_host.get(candidate.hostname)
            if current is None:
                merged_by_host[candidate.hostname] = candidate
                continue
            merged_by_host[candidate.hostname] = current.model_copy(
                update={
                    "first_seen": current.first_seen or candidate.first_seen,
                    "label_hint": current.label_hint or candidate.label_hint,
                    "discovery_rank": max(current.discovery_rank, candidate.discovery_rank),
                    "discovery_evidence_urls": list(
                        dict.fromkeys(
                            [
                                *current.discovery_evidence_urls,
                                *candidate.discovery_evidence_urls,
                            ]
                        )
                    )[:30],
                    "provider_record_ids": list(
                        dict.fromkeys(
                            [
                                *current.provider_record_ids,
                                *candidate.provider_record_ids,
                            ]
                        )
                    )[:30],
                    "discovery_assertions": self._merge_discovery_assertions(
                        current.discovery_assertions,
                        candidate.discovery_assertions,
                    ),
                }
            )

        hostnames = list(merged_by_host)
        existing: dict[str, sqlite3.Row] = {}
        for start in range(0, len(hostnames), 500):
            batch = hostnames[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT source_apex, hostname, first_seen, label_hint, discovery_rank,
                       discovery_evidence_json, provider_record_ids_json,
                       discovery_assertions_json
                FROM candidates WHERE hostname IN ({placeholders})
                """,
                batch,
            ).fetchall()
            existing.update({str(row["hostname"]): row for row in rows})

        merged: list[DiscoveredHost] = []
        for hostname, candidate in merged_by_host.items():
            row = existing.get(hostname)
            if row is None:
                merged.append(candidate)
                continue
            old_first_seen = (
                datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None
            )
            merged.append(
                candidate.model_copy(
                    update={
                        # The legacy origin remains stable. Every additional discovery
                        # path is retained below in candidate_source_edges.
                        "source_apex": str(row["source_apex"]),
                        "first_seen": old_first_seen or candidate.first_seen,
                        "label_hint": str(row["label_hint"] or candidate.label_hint),
                        "discovery_rank": max(
                            float(row["discovery_rank"] or 0),
                            candidate.discovery_rank,
                        ),
                        "discovery_evidence_urls": list(
                            dict.fromkeys(
                                [
                                    *json.loads(row["discovery_evidence_json"] or "[]"),
                                    *candidate.discovery_evidence_urls,
                                ]
                            )
                        )[:30],
                        "provider_record_ids": list(
                            dict.fromkeys(
                                [
                                    *json.loads(row["provider_record_ids_json"] or "[]"),
                                    *candidate.provider_record_ids,
                                ]
                            )
                        )[:30],
                        "discovery_assertions": self._merge_discovery_assertions(
                            json.loads(row["discovery_assertions_json"] or "[]"),
                            candidate.discovery_assertions,
                        ),
                    }
                )
            )
        return merged

    def _upsert_candidate_source_edges_uncommitted(
        self,
        candidates: list[DiscoveredHost],
        *,
        timestamp: str,
    ) -> None:
        """Persist every source-to-company path, including repeated discoveries."""

        if not candidates:
            return
        candidate_ids = self.candidate_ids(
            list(dict.fromkeys(candidate.hostname for candidate in candidates))
        )
        accumulated: dict[tuple[int, str], dict[str, Any]] = {}
        for candidate in candidates:
            candidate_id = candidate_ids.get(candidate.hostname)
            source_apex = candidate.source_apex.strip().lower().rstrip(".")
            if candidate_id is None or not source_apex:
                continue
            key = (candidate_id, source_apex)
            value = accumulated.setdefault(
                key,
                {
                    "rank": 0.0,
                    "label": "",
                    "urls": [],
                    "ids": [],
                    "assertions": [],
                },
            )
            value["rank"] = max(float(value["rank"]), candidate.discovery_rank)
            value["label"] = str(value["label"] or candidate.label_hint)
            value["urls"] = list(
                dict.fromkeys([*value["urls"], *candidate.discovery_evidence_urls])
            )[:30]
            value["ids"] = list(dict.fromkeys([*value["ids"], *candidate.provider_record_ids]))[:30]
            value["assertions"] = self._merge_discovery_assertions(
                value["assertions"],
                candidate.discovery_assertions,
            )

        if not accumulated:
            return
        candidate_id_values = list(dict.fromkeys(key[0] for key in accumulated))
        existing: dict[tuple[int, str], sqlite3.Row] = {}
        for start in range(0, len(candidate_id_values), 500):
            batch = candidate_id_values[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT * FROM candidate_source_edges
                WHERE candidate_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            existing.update(
                {(int(row["candidate_id"]), str(row["source_apex"])): row for row in rows}
            )

        rows_to_write = []
        for (candidate_id, source_apex), value in accumulated.items():
            old = existing.get((candidate_id, source_apex))
            old_urls = json.loads(old["evidence_urls_json"] or "[]") if old else []
            old_ids = json.loads(old["provider_record_ids_json"] or "[]") if old else []
            old_assertions = json.loads(old["discovery_assertions_json"] or "[]") if old else []
            rows_to_write.append(
                (
                    candidate_id,
                    source_apex,
                    max(float(old["discovery_rank"] or 0), float(value["rank"]))
                    if old
                    else float(value["rank"]),
                    str((old["label_hint"] if old else "") or value["label"]),
                    json.dumps(list(dict.fromkeys([*old_urls, *value["urls"]]))[:30]),
                    json.dumps(list(dict.fromkeys([*old_ids, *value["ids"]]))[:30]),
                    self._discovery_assertions_json(
                        self._merge_discovery_assertions(
                            old_assertions,
                            value["assertions"],
                        )
                    ),
                    str(old["first_seen_at"]) if old else timestamp,
                    timestamp,
                )
            )
        self.connection.executemany(
            """
            INSERT INTO candidate_source_edges(
                candidate_id, source_apex, discovery_rank, label_hint,
                evidence_urls_json, provider_record_ids_json,
                discovery_assertions_json,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, source_apex) DO UPDATE SET
                discovery_rank=excluded.discovery_rank,
                label_hint=excluded.label_hint,
                evidence_urls_json=excluded.evidence_urls_json,
                provider_record_ids_json=excluded.provider_record_ids_json,
                discovery_assertions_json=excluded.discovery_assertions_json,
                last_seen_at=excluded.last_seen_at
            """,
            rows_to_write,
        )

    def _admit_company_hosts(self, hosts) -> None:
        inherited_transaction = self.connection.in_transaction
        detected = register_hosts(self.connection, hosts)
        if not inherited_transaction:
            self.connection.commit()
        # Admission may publish a conservative blocking rule, but it must never commit
        # unrelated caller-owned work or destroy the caller's savepoint. Reconciliation
        # runs at the next owned maintenance/store boundary after that transaction ends.
        if detected and not inherited_transaction:
            reconcile_disallowed_domains(self.connection)

    def upsert_candidate(self, candidate: DiscoveredHost) -> int:
        self._admit_company_hosts([candidate.hostname])
        require_allowed(candidate.hostname)
        timestamp = _now()
        stored = self._merge_candidates_for_storage([candidate])[0]
        self.connection.execute(
            """
            INSERT INTO candidates(
                source_apex, hostname, first_seen, label_hint, discovery_rank,
                discovery_evidence_json, provider_record_ids_json,
                discovery_assertions_json,
                discovered_at, last_discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hostname) DO UPDATE SET
                first_seen=COALESCE(candidates.first_seen, excluded.first_seen),
                label_hint=CASE WHEN candidates.label_hint=''
                    THEN excluded.label_hint ELSE candidates.label_hint END,
                discovery_rank=MAX(candidates.discovery_rank, excluded.discovery_rank),
                discovery_evidence_json=excluded.discovery_evidence_json,
                provider_record_ids_json=excluded.provider_record_ids_json,
                discovery_assertions_json=excluded.discovery_assertions_json,
                last_discovered_at=excluded.last_discovered_at
            """,
            (
                stored.source_apex,
                stored.hostname,
                stored.first_seen.isoformat() if stored.first_seen else None,
                stored.label_hint,
                stored.discovery_rank,
                json.dumps(stored.discovery_evidence_urls),
                json.dumps(stored.provider_record_ids),
                self._discovery_assertions_json(stored.discovery_assertions),
                timestamp,
                timestamp,
            ),
        )
        self._upsert_candidate_source_edges_uncommitted([candidate], timestamp=timestamp)
        row = self.connection.execute(
            "SELECT id FROM candidates WHERE hostname=?", (candidate.hostname,)
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def upsert_candidates(self, candidates: list[DiscoveredHost], *, commit: bool = True) -> int:
        """Persist a discovery inventory in one transaction before inspection."""
        self._admit_company_hosts(candidate.hostname for candidate in candidates)
        candidates = [
            candidate for candidate in candidates if not is_disallowed(candidate.hostname)
        ]
        if not candidates:
            return 0
        timestamp = _now()
        stored_candidates = self._merge_candidates_for_storage(candidates)
        self.connection.executemany(
            """
            INSERT INTO candidates(
                source_apex, hostname, first_seen, label_hint, discovery_rank,
                discovery_evidence_json, provider_record_ids_json,
                discovery_assertions_json,
                discovered_at, last_discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hostname) DO UPDATE SET
                first_seen=COALESCE(candidates.first_seen, excluded.first_seen),
                label_hint=CASE WHEN candidates.label_hint=''
                    THEN excluded.label_hint ELSE candidates.label_hint END,
                discovery_rank=MAX(candidates.discovery_rank, excluded.discovery_rank),
                discovery_evidence_json=excluded.discovery_evidence_json,
                provider_record_ids_json=excluded.provider_record_ids_json,
                discovery_assertions_json=excluded.discovery_assertions_json,
                last_discovered_at=excluded.last_discovered_at
            """,
            [
                (
                    candidate.source_apex,
                    candidate.hostname,
                    candidate.first_seen.isoformat() if candidate.first_seen else None,
                    candidate.label_hint,
                    candidate.discovery_rank,
                    json.dumps(candidate.discovery_evidence_urls),
                    json.dumps(candidate.provider_record_ids),
                    self._discovery_assertions_json(candidate.discovery_assertions),
                    timestamp,
                    timestamp,
                )
                for candidate in stored_candidates
            ],
        )
        self._upsert_candidate_source_edges_uncommitted(candidates, timestamp=timestamp)
        if commit:
            self.connection.commit()
        return len(candidates)

    def upsert_derived_candidates(self, candidates: list[DiscoveredHost]) -> int:
        """Add provider/relationship discoveries without rewriting an existing origin."""
        self._admit_company_hosts(candidate.hostname for candidate in candidates)
        candidates = [
            candidate for candidate in candidates if not is_disallowed(candidate.hostname)
        ]
        if not candidates:
            return 0
        timestamp = _now()
        existing: dict[str, tuple[list[str], list[str], list[dict[str, Any]]]] = {}
        hostnames = list(dict.fromkeys(candidate.hostname for candidate in candidates))
        for start in range(0, len(hostnames), 500):
            batch = hostnames[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT hostname, discovery_evidence_json, provider_record_ids_json,
                       discovery_assertions_json
                FROM candidates WHERE hostname IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                existing[str(row["hostname"])] = (
                    json.loads(row["discovery_evidence_json"] or "[]"),
                    json.loads(row["provider_record_ids_json"] or "[]"),
                    json.loads(row["discovery_assertions_json"] or "[]"),
                )
        merged_candidates = []
        for candidate in candidates:
            old_urls, old_ids, old_assertions = existing.get(candidate.hostname, ([], [], []))
            merged_candidates.append(
                candidate.model_copy(
                    update={
                        "discovery_evidence_urls": list(
                            dict.fromkeys([*old_urls, *candidate.discovery_evidence_urls])
                        )[:20],
                        "provider_record_ids": list(
                            dict.fromkeys([*old_ids, *candidate.provider_record_ids])
                        )[:20],
                        "discovery_assertions": self._merge_discovery_assertions(
                            old_assertions,
                            candidate.discovery_assertions,
                        ),
                    }
                )
            )
        self.connection.executemany(
            """
            INSERT INTO candidates(
                source_apex, hostname, first_seen, label_hint, discovery_rank,
                discovery_evidence_json, provider_record_ids_json,
                discovery_assertions_json,
                discovered_at, last_discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hostname) DO UPDATE SET
                first_seen=COALESCE(candidates.first_seen, excluded.first_seen),
                label_hint=CASE
                    WHEN candidates.label_hint='' THEN excluded.label_hint
                    ELSE candidates.label_hint
                END,
                discovery_rank=MAX(candidates.discovery_rank, excluded.discovery_rank),
                discovery_evidence_json=excluded.discovery_evidence_json,
                provider_record_ids_json=excluded.provider_record_ids_json,
                discovery_assertions_json=excluded.discovery_assertions_json,
                last_discovered_at=excluded.last_discovered_at
            """,
            [
                (
                    candidate.source_apex,
                    candidate.hostname,
                    candidate.first_seen.isoformat() if candidate.first_seen else None,
                    candidate.label_hint,
                    candidate.discovery_rank,
                    json.dumps(candidate.discovery_evidence_urls),
                    json.dumps(candidate.provider_record_ids),
                    self._discovery_assertions_json(candidate.discovery_assertions),
                    timestamp,
                    timestamp,
                )
                for candidate in merged_candidates
            ],
        )
        self._upsert_candidate_source_edges_uncommitted(candidates, timestamp=timestamp)
        self.connection.commit()
        return len(candidates)

    def discovery_backlog(
        self, source_apexes: list[str], *, limit: int = 20_000
    ) -> list[DiscoveredHost]:
        """Return persisted provider/derived candidates, prioritizing never-inspected hosts."""
        if not source_apexes or limit <= 0:
            return []
        placeholders = ", ".join("?" for _ in source_apexes)
        rows = self.connection.execute(
            f"""
            SELECT c.source_apex, c.hostname, c.first_seen, c.discovery_rank, c.label_hint,
                   c.discovery_evidence_json, c.provider_record_ids_json,
                   c.discovery_assertions_json,
                   MAX(s.observed_at) AS last_observed_at
            FROM candidates c
            LEFT JOIN snapshots s ON s.candidate_id=c.id
            WHERE c.source_apex IN ({placeholders})
            GROUP BY c.id
            ORDER BY CASE WHEN MAX(s.id) IS NULL THEN 0 ELSE 1 END,
                     c.discovery_rank DESC, c.last_discovered_at DESC
            LIMIT ?
            """,
            [*source_apexes, limit],
        ).fetchall()
        return [
            DiscoveredHost(
                source_apex=str(row["source_apex"]),
                hostname=str(row["hostname"]),
                first_seen=(
                    datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None
                ),
                discovery_rank=float(row["discovery_rank"] or 0),
                label_hint=str(row["label_hint"] or ""),
                discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
                provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
                discovery_assertions=json.loads(row["discovery_assertions_json"] or "[]"),
            )
            for row in rows
        ]

    def candidates_by_hosts(self, hosts: list[str]) -> list[DiscoveredHost]:
        """Load only exact persisted hosts, preserving request order and provenance."""
        normalized_hosts = list(
            dict.fromkeys(
                str(host).strip().lower().rstrip(".") for host in hosts if str(host).strip()
            )
        )
        if not normalized_hosts:
            return []
        found: dict[str, sqlite3.Row] = {}
        for start in range(0, len(normalized_hosts), 500):
            batch = normalized_hosts[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT source_apex, hostname, first_seen, discovery_rank, label_hint,
                       discovery_evidence_json, provider_record_ids_json,
                       discovery_assertions_json
                FROM candidates
                WHERE hostname IN ({placeholders})
                """,
                batch,
            ).fetchall()
            found.update({str(row["hostname"]): row for row in rows})
        return [
            DiscoveredHost(
                source_apex=str(row["source_apex"]),
                hostname=str(row["hostname"]),
                first_seen=(
                    datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None
                ),
                discovery_rank=float(row["discovery_rank"] or 0),
                label_hint=str(row["label_hint"] or ""),
                discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
                provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
                discovery_assertions=json.loads(row["discovery_assertions_json"] or "[]"),
            )
            for host in normalized_hosts
            if (row := found.get(host)) is not None
        ]

    def candidate_ids(self, hostnames: list[str]) -> dict[str, int]:
        if not hostnames:
            return {}
        identifiers: dict[str, int] = {}
        unique_hosts = list(dict.fromkeys(hostnames))
        for start in range(0, len(unique_hosts), 500):
            batch = unique_hosts[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"SELECT id, hostname FROM candidates WHERE hostname IN ({placeholders})",
                batch,
            ).fetchall()
            identifiers.update({str(row["hostname"]): int(row["id"]) for row in rows})
        return identifiers

    def candidate_company_keys(self, candidate_ids: list[int]) -> dict[int, str]:
        """Resolve aliases to the canonical identity used by company/scanner leases."""

        return self._candidate_company_keys(self._normalized_candidate_ids(candidate_ids))

    @staticmethod
    def _suggestion_hydration_profile(row: sqlite3.Row) -> dict[str, Any]:
        source_kind = str(row["source_kind"] or "")
        category = str(row["category"] or "")
        row_keys = set(row.keys())
        relationship_type = (
            str(row["relationship_type"] or "") if "relationship_type" in row_keys else ""
        )
        if relationship_type in TARGET_RELATIONSHIP_LANES:
            lane = relationship_type
        elif source_kind == "agency_directory" or category == "Agency":
            # Defensive fallback for an unmigrated row shape supplied outside LeadStore.
            lane = "referral_partner"
        else:
            lane = "buyer"

        if (
            lane == "referral_partner"
            or source_kind
            in {
                "brand_directory",
                "brand_group_portfolio",
                "platform_customer_list",
                "agency_directory",
            }
            or category
            in {
                "Agency",
                "Ecommerce",
                "Consumer / Digital",
                "Brand group",
            }
        ):
            scope = "direct"
        elif category == "B2B / SaaS":
            scope = "adjacency"
        else:
            scope = "unclassified"
        fit_weight = {"medium": 2, "high": 4, "very_high": 6}.get(
            str(row["expected_6k_fit"] or ""), 1
        )
        confidence_bonus = {"low": 0, "medium": 1, "high": 2}.get(str(row["confidence"] or ""), 0)
        intelligence_bonus = max(0, min(int(row["intelligence_score"] or 0) // 25, 3))
        suggestion_id = int(row["suggestion_id"])
        identity_key = str(row["source_identity_key"] or f"suggestion:{suggestion_id}")
        return {
            "relationship_lane": lane,
            "target_scope": scope,
            "weight": min(10, fit_weight + confidence_bonus + intelligence_bonus),
            "allocation_key": f"suggestion:{identity_key}",
            "source_kind": source_kind,
            "source_id": suggestion_id,
        }

    def candidate_hydration_profiles(self, hostnames: list[str]) -> dict[str, dict[str, Any]]:
        """Resolve edge-level scan intent without mutating a candidate's company identity.

        A candidate may be discovered through several sources. Executable source intent is
        stronger than a generic source hostname, and independent paths add only a small
        corroboration bonus rather than multiplying scan allocation without bound.
        """

        normalized = list(
            dict.fromkeys(
                str(host).strip().lower().rstrip(".") for host in hostnames if str(host).strip()
            )
        )
        if not normalized:
            return {}
        profiles_by_host: dict[str, list[dict[str, Any]]] = {host: [] for host in normalized}
        for start in range(0, len(normalized), 400):
            batch = normalized[start : start + 400]
            placeholders = ", ".join("?" for _ in batch)
            source_rows = self.connection.execute(
                f"""
                SELECT c.hostname, e.source_apex
                FROM candidates c
                JOIN candidate_source_edges e ON e.candidate_id=c.id
                WHERE c.hostname IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in source_rows:
                source_apex = str(row["source_apex"])
                profile = source_hydration_profile(source_apex)
                profiles_by_host[str(row["hostname"])].append(
                    {
                        "relationship_lane": profile.relationship_lane,
                        "target_scope": profile.target_scope,
                        "weight": profile.weight,
                        "allocation_key": f"source:{source_apex}",
                        "source_kind": "source_apex",
                        "source_id": source_apex,
                    }
                )
            suggestion_rows = self.connection.execute(
                f"""
                SELECT DISTINCT c.hostname, ss.id AS suggestion_id,
                       ss.source_kind, ss.category, ss.expected_6k_fit,
                       ss.confidence, ss.intelligence_score,
                       ss.relationship_type, ss.source_identity_key
                FROM candidates c
                JOIN discovery_source_candidates d ON d.candidate_id=c.id
                JOIN source_suggestions member_source ON member_source.id=d.suggestion_id
                JOIN source_suggestions ss
                  ON ss.id=COALESCE(member_source.duplicate_of_suggestion_id,
                                    member_source.id)
                WHERE c.hostname IN ({placeholders})
                  AND ss.status<>'rejected'
                """,
                batch,
            ).fetchall()
            for row in suggestion_rows:
                profiles_by_host[str(row["hostname"])].append(
                    self._suggestion_hydration_profile(row)
                )

        resolved: dict[str, dict[str, Any]] = {}
        for host, profiles in profiles_by_host.items():
            if not profiles:
                continue

            # Prefer direct-buyer intent, then a referral/agency path, then adjacency.
            def priority(profile: dict[str, Any]) -> tuple[int, int]:
                lane = profile["relationship_lane"]
                scope = profile["target_scope"]
                intent = (
                    3
                    if lane == "buyer" and scope == "direct"
                    else 2
                    if lane == "referral_partner"
                    else 1
                    if scope == "adjacency"
                    else 0
                )
                return intent, int(profile["weight"])

            strongest = max(profiles, key=priority)
            independent_paths = len({str(profile["allocation_key"]) for profile in profiles})
            resolved[host] = {
                **strongest,
                "weight": min(
                    10,
                    int(strongest["weight"]) + min(2, independent_paths - 1),
                ),
                "independent_source_paths": independent_paths,
                "source_paths": [
                    str(profile["allocation_key"])
                    for profile in sorted(profiles, key=priority, reverse=True)
                ][:12],
            }
        return resolved

    @staticmethod
    def _material_payload(observation: SiteObservation, score: ScoreCard) -> dict[str, Any]:
        return {
            "live": observation.live,
            "final_url": observation.final_url,
            "status_code": observation.status_code,
            "title": observation.title,
            "content_hash": observation.content_hash,
            "emails": observation.emails,
            "contacts": observation.contacts.model_dump(mode="json"),
            "contact_urls": observation.contact_urls,
            "social_urls": observation.social_urls,
            "signals": observation.signals,
            "technologies": observation.technologies,
            "web": observation.web.model_dump(mode="json"),
            "score": score.model_dump(mode="json"),
        }

    def save_snapshot(
        self,
        run_id: int,
        candidate_id: int,
        observation: SiteObservation,
        score: ScoreCard,
        ai: AIAnalysis | None,
        final_score: int,
        *,
        complete_company_work: bool = False,
        failed_company_work: bool = False,
    ) -> list[dict[str, Any]]:
        """Publish one snapshot and all derived rows atomically, preserving caller work."""
        if complete_company_work and failed_company_work:
            raise ValueError("Company work cannot complete and fail in the same checkpoint")
        self._admit_company_hosts([observation.hostname, observation.canonical_host])
        require_allowed(observation.hostname)
        require_allowed(observation.canonical_host)
        nested = self.connection.in_transaction
        savepoint = f"snapshot_{uuid4().hex}"
        self.connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            result = self._save_snapshot_uncommitted(
                run_id, candidate_id, observation, score, ai, final_score,
                complete_company_work=complete_company_work,
                failed_company_work=failed_company_work,
            )
            if nested:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.commit()
            return result
        except BaseException:
            if nested:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.rollback()
            raise

    def _save_snapshot_uncommitted(
        self,
        run_id: int,
        candidate_id: int,
        observation: SiteObservation,
        score: ScoreCard,
        ai: AIAnalysis | None,
        final_score: int,
        *,
        complete_company_work: bool,
        failed_company_work: bool,
    ) -> list[dict[str, Any]]:
        previous = self.connection.execute(
            """
            SELECT s.observation_json, s.score_json, s.final_score
            FROM snapshots s
            WHERE s.candidate_id=? AND s.run_id<>?
            ORDER BY s.id DESC LIMIT 1
            """,
            (candidate_id, run_id),
        ).fetchone()
        material = self._material_payload(observation, score)
        material_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.connection.execute(
            """
            INSERT INTO snapshots(
                candidate_id, run_id, observed_at, observation_json, score_json,
                ai_json, final_score, material_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, run_id) DO UPDATE SET
                observed_at=excluded.observed_at,
                observation_json=excluded.observation_json,
                score_json=excluded.score_json,
                ai_json=excluded.ai_json,
                final_score=excluded.final_score,
                material_hash=excluded.material_hash
            """,
            (
                candidate_id,
                run_id,
                observation.observed_at.isoformat(),
                encode_storage_json(observation.model_dump_json(), "observation"),
                encode_storage_json(score.model_dump_json(), "score"),
                ai.model_dump_json() if ai else None,
                final_score,
                material_hash,
            ),
        )
        projection_row = self.connection.execute(
            """
            SELECT s.id AS snapshot_id, c.source_apex, c.hostname, c.first_seen,
                   c.discovery_rank, c.label_hint, c.discovery_evidence_json,
                   c.provider_record_ids_json, c.discovery_assertions_json
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            WHERE s.candidate_id=? AND s.run_id=?
            """,
            (candidate_id, run_id),
        ).fetchone()
        if projection_row is None:  # pragma: no cover - guarded by the upsert above
            raise RuntimeError("Saved snapshot could not be projected")
        projection_candidate = DiscoveredHost(
            source_apex=projection_row["source_apex"],
            hostname=projection_row["hostname"],
            first_seen=(
                datetime.fromisoformat(projection_row["first_seen"])
                if projection_row["first_seen"]
                else None
            ),
            discovery_rank=float(projection_row["discovery_rank"] or 0),
            label_hint=projection_row["label_hint"] or "",
            discovery_evidence_urls=json.loads(projection_row["discovery_evidence_json"] or "[]"),
            provider_record_ids=json.loads(projection_row["provider_record_ids_json"] or "[]"),
            discovery_assertions=json.loads(projection_row["discovery_assertions_json"] or "[]"),
        )
        self._upsert_snapshot_target_intelligence_uncommitted(
            snapshot_id=int(projection_row["snapshot_id"]),
            candidate_id=candidate_id,
            candidate=projection_candidate,
            observation=observation,
            score=score,
        )
        changes = self._detect_changes(previous, observation, score, final_score)
        self.connection.execute(
            "DELETE FROM changes WHERE candidate_id=? AND run_id=?", (candidate_id, run_id)
        )
        for change in changes:
            self.connection.execute(
                """
                INSERT INTO changes(candidate_id, run_id, kind, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (candidate_id, run_id, change["kind"], json.dumps(change), _now()),
            )
        if complete_company_work:
            self._complete_company_claim_uncommitted(
                candidate_id,
                run_id,
                effort_key=COMPANY_SCAN_EFFORT,
                completed_at=(observation.observed_at.isoformat()
                              if observation.core_hydration_reused else _now()),
            )
        elif failed_company_work:
            timestamp = _now()
            retry_at = (datetime.fromisoformat(timestamp) + timedelta(
                hours=COMPANY_SCAN_FAILURE_RETRY_HOURS
            )).isoformat()
            self.connection.execute(
                """
                UPDATE company_scan_work
                SET status='failed', claimed_candidate_id=NULL, claimed_run_id=NULL,
                    claimed_at=NULL, lease_expires_at=NULL, next_retry_at=?,
                    last_error=?, updated_at=?
                WHERE effort_key=? AND status='running'
                  AND claimed_run_id=? AND claimed_candidate_id=?
                """,
                (retry_at, (observation.error or "; ".join(observation.core_hydration_errors)
                            or "incomplete_core_hydration")[:2_000],
                 timestamp, COMPANY_SCAN_EFFORT, run_id, candidate_id),
            )
        return changes

    def time_diverse_snapshot_history(
        self, candidate_id: int, *, before_id: int, reference_time: datetime | None = None,
        limit: int = 24, company_key: str | None = None, exhaustive: bool = False,
    ) -> list[SiteObservation]:
        """Read at most 24 time-diverse, identity-matched company observations.

        Daily buckets prevent repeated imports crowding out older dates. Current
        aliases share history; another company's pre-redirect history is never
        relabelled as this company's evidence. Original observations stay unchanged.
        """
        now = reference_time or datetime.now(UTC)
        limit = max(2, min(int(limit), 24))
        ids = [candidate_id]
        if company_key:
            ids.extend(r[0] for r in self.connection.execute(
                "SELECT candidate_id FROM engine_ranking_members WHERE company_key=?",
                (company_key,)))
        ids = sorted(set(ids))
        placeholders = ",".join("?" for _ in ids)
        identity_clause = " AND ti.canonical_domain=?" if company_key else ""
        rows = self.connection.execute(
            ("SELECT s.id,s.observed_at FROM snapshots s " if exhaustive else
             "SELECT MAX(s.id) AS id,MAX(s.observed_at) AS observed_at FROM snapshots s ") +
            "JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
            f"WHERE s.candidate_id IN ({placeholders}) "
            "AND (s.candidate_id<>? OR s.id<?) "
            "AND s.observed_at>=? AND s.observed_at<=?" + identity_clause +
            (" ORDER BY s.observed_at DESC,s.id DESC" if exhaustive else
             " GROUP BY substr(s.observed_at,1,10) ORDER BY observed_at DESC LIMIT 121"),
            (*ids, candidate_id, before_id,
             (now - timedelta(days=365 if exhaustive else 120)).isoformat(), now.isoformat(),
             *((company_key,) if company_key else ())),
        ).fetchall()
        if not exhaustive and len(rows) > limit:
            rows = [rows[round(i * (len(rows) - 1) / (limit - 1))] for i in range(limit)]
        result = []
        # Read the selected observations in bounded groups, then restore the selection order.
        batch_size = 32
        for start in range(0, len(rows), batch_size):
            snapshot_ids = [row["id"] for row in rows[start:start + batch_size]]
            bindings = ",".join("?" for _ in snapshot_ids)
            saved_rows = {
                saved["id"]: saved for saved in self.connection.execute(
                    "SELECT s.id,s.observation_json,c.hostname,c.source_apex FROM snapshots s "
                    "JOIN candidates c ON c.id=s.candidate_id "
                    f"WHERE s.id IN ({bindings})", snapshot_ids,
                )
            }
            for snapshot_id in snapshot_ids:
                saved = saved_rows[snapshot_id]
                observation = SiteObservation.model_validate_json(saved["observation_json"])
                identity = DiscoveredHost(
                    hostname=saved["hostname"], source_apex=saved["source_apex"],
                )
                if (
                    company_key
                    and self._target_canonical_domain(identity, observation) != company_key
                ):
                    continue
                result.append(observation)
        return result

    def scoring_snapshot(self, candidate_id: int, *, reference_time: datetime | None = None):
        """Retain recent same-identity evidence through retry-only retrieval failures.

        A failed request does not establish closure. Permanent outcomes and changed identities
        terminate the search. Neither the retained source date nor any raw snapshot is changed.
        """
        now = self._utc_datetime(reference_time or datetime.now(UTC))
        query = """
            SELECT s.id AS snapshot_id, s.observation_json, s.observed_at,
                   c.source_apex, c.hostname, c.first_seen, c.discovery_rank,
                   c.label_hint, c.discovery_evidence_json, c.provider_record_ids_json,
                   c.discovery_assertions_json
            FROM snapshots s JOIN candidates c ON c.id=s.candidate_id
            WHERE s.candidate_id=?
        """
        latest = self.connection.execute(query + " ORDER BY s.id DESC LIMIT 1",
                                         (candidate_id,)).fetchone()
        if latest is None:
            return None
        candidate = DiscoveredHost(hostname=latest["hostname"], source_apex=latest["source_apex"])
        current = SiteObservation.model_validate_json(latest["observation_json"])
        if current.live or current.fetch_outcome not in {"retryable_error", "circuit_open"}:
            return latest
        company = self._target_canonical_domain(candidate, current)
        for row in self.connection.execute(
            query + " AND s.id<? AND s.observed_at>? AND s.observed_at<=? ORDER BY s.id DESC",
            (candidate_id, latest["snapshot_id"], (now - timedelta(days=90)).isoformat(),
             now.isoformat()),
        ):
            prior = SiteObservation.model_validate_json(row["observation_json"])
            if self._target_canonical_domain(candidate, prior) != company:
                break
            if prior.live:
                return row
            if prior.fetch_outcome not in {"retryable_error", "circuit_open"}:
                break
        return latest

    def current_candidate_score(
        self, candidate_id: int, *, reference_time: datetime | None = None,
    ):
        """Calculate from current saved ledgers without rewriting any observations."""
        from .sales_engine.score_cache import evidence_key, get_score, put_score

        reference_time = reference_time or datetime.now(UTC)
        row = self.scoring_snapshot(candidate_id, reference_time=reference_time)
        if row is None:
            return None
        previous = self.connection.execute(
            """
            SELECT observation_json FROM snapshots
            WHERE candidate_id=? AND id<? ORDER BY id DESC LIMIT 1
            """,
            (candidate_id, row["snapshot_id"]),
        ).fetchone()
        candidate = DiscoveredHost(
            source_apex=row["source_apex"],
            hostname=row["hostname"],
            first_seen=(datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None),
            discovery_rank=float(row["discovery_rank"] or 0),
            label_hint=row["label_hint"] or "",
            discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
            provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
            discovery_assertions=json.loads(row["discovery_assertions_json"] or "[]"),
        )
        observation = SiteObservation.model_validate_json(row["observation_json"])
        company_key = self._target_canonical_domain(candidate, observation)
        current_ads = self.current_candidate_ad_intelligence(
            candidate_id, company_key=company_key, reference_time=reference_time)
        cache_key = evidence_key(self, candidate_id, company_key, row, current_ads)
        cached = get_score(cache_key, reference_time)
        if cached is not None:
            return row, candidate, *cached
        observation = observation.model_copy(
            update={
                "web": observation.web.model_copy(
                    update={"frontier": self.frontier_intelligence(
                        company_key, as_of=reference_time, execution_limit=None
                    )}
                )
            }
        )
        from .buyer_intelligence import canonical_asset, load_evidence
        from .sales_engine.hydrated_scoring import (
            load_supplemental_events,
            load_supplemental_inputs,
        )
        from .scoring import score_candidate

        # Existing paid creatives suppress duplicate creator-content production evidence.
        paid_keys = set()
        for creative in self.connection.execute(
            "SELECT source_url,provenance_json FROM ad_creatives WHERE landing_domain=? "
            "AND last_seen_at>=? AND last_seen_at<=?",
            (company_key, (reference_time - timedelta(days=90)).isoformat(),
             reference_time.isoformat()),
        ):
            provenance = json.loads(creative["provenance_json"] or "{}")
            for value in [creative["source_url"], *(provenance.get("media_urls") or []),
                          provenance.get("original_post_url"), provenance.get("video_url")]:
                if canonical_asset(value):
                    paid_keys.add(canonical_asset(value))
        observation = observation.model_copy(update={
            "canonical_host": company_key,
            "buyer_evidence": [*observation.buyer_evidence,
                               *load_evidence(self.connection, company_key, now=reference_time)],
            "paid_creative_asset_keys": sorted(paid_keys),
        })
        from .microlink_hydration import public_contacts

        observation.contacts = type(observation.contacts).model_validate(public_contacts(
            self.connection, company_key, observation.contacts.model_dump(mode="json"),
            now=reference_time,
        ))
        history = [item.model_copy(update={"canonical_host": company_key}) for item in
                   self.time_diverse_snapshot_history(
                       candidate_id, before_id=row["snapshot_id"], reference_time=reference_time,
                       company_key=company_key, exhaustive=True)]
        previous_observation = (
            SiteObservation.model_validate_json(previous["observation_json"]) if previous else None)
        if previous_observation and self._target_canonical_domain(
            candidate, previous_observation
        ) != company_key:
            previous_observation = None
        supplemental_inputs = load_supplemental_inputs(self, observation, now=reference_time)
        supplemental_page_scores = {}
        score_inputs = dict(
            candidate=candidate,
            observation=observation,
            reference_time=reference_time,
            supplemental_events=load_supplemental_events(
                self, candidate_id, observation, history, now=reference_time,
                inputs=supplemental_inputs, page_scores=supplemental_page_scores),
            previous_observation=previous_observation,
            ad_intelligence=AdIntelligence.model_validate(current_ads),
        )
        score = score_candidate(**score_inputs)
        from .sales_engine.evidence_accounting import build_accounting, summary
        from .sales_engine.source_sensitivity import (
            identical_score_inputs,
            measure_sources,
            without_source,
        )

        def prepare_replay(source):
            replay_observation = without_source(observation, source, for_scoring=True)
            replay_history = [without_source(item, source, for_scoring=True) for item in history]
            return dict(
                candidate=candidate, observation=replay_observation,
                reference_time=reference_time, trace=False,
                supplemental_events=load_supplemental_events(
                    self, candidate_id, replay_observation, replay_history,
                    now=reference_time, excluded_source=source,
                    inputs=supplemental_inputs, page_scores=supplemental_page_scores,
                ),
                previous_observation=(without_source(previous_observation, source, for_scoring=True)
                                      if previous_observation else None),
                ad_intelligence=(AdIntelligence() if source == "advertising" else
                                 AdIntelligence.model_validate(current_ads)),
            )

        def replay(source):
            prepared = prepare_replay(source)
            return score if identical_score_inputs(prepared, score_inputs) else score_candidate(
                **prepared
            )

        from .sales_engine import synthesis
        # All eight removals are measured. Exact input matches need no repeated computation;
        # changed inputs retain the full replay, including the bounded process-worker path.
        if synthesis.enabled() and len(history) >= 20:
            from .sales_engine.source_sensitivity import SOURCES
            effects = synthesis.measure(
                score.creative_capacity.opportunity,
                ((source, prepare_replay(source)) for source in SOURCES), replay,
                baseline_inputs=score_inputs,
            )
        else:
            effects = measure_sources(score.creative_capacity.opportunity, replay)
        score.creative_capacity.opportunity.source_effects = effects
        score.creative_capacity.opportunity.evidence_accounting = build_accounting(
            self, company_key, row, observation, score, now=reference_time
        )
        score.creative_capacity.opportunity.evidence_accounting_summary = summary(
            score.creative_capacity.opportunity.evidence_accounting
        )
        microlink_boundaries = [page.observed_at + timedelta(days=30)
                               for _, page in supplemental_inputs.microlink_pages]
        microlink_boundaries.extend(supplemental_inputs.microlink_boundaries)
        if microlink_boundaries:
            score.creative_capacity.opportunity.freshness_audit["microlink_valid_until"] = min(
                microlink_boundaries).isoformat()
        put_score(cache_key, reference_time, observation, score,
                  boundaries=microlink_boundaries)
        return row, candidate, observation, score

    def reproject_candidate_ad_intelligence(
        self, candidate_id: int, *, reference_time: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        """Refresh the derived projection through the same scorer used by Sales."""
        from .desktop_ranking.control import control_for, reproject

        if (control := control_for(self)) is not None:
            if not commit:
                raise ValueError("Desktop projection requires committed evidence")
            return candidate_id in reproject(self, control, [candidate_id], now=reference_time)
        current = self.current_candidate_score(candidate_id, reference_time=reference_time)
        if current is None:
            return False
        row, candidate, observation, score = current
        self._upsert_snapshot_target_intelligence_uncommitted(
            snapshot_id=int(row["snapshot_id"]),
            candidate_id=candidate_id,
            candidate=candidate,
            observation=observation,
            score=score,
            computed_at=reference_time.isoformat() if reference_time else None,
        )
        if commit:
            self.connection.commit()
        return True

    def reproject_candidate_intelligence(self, candidate_id: int) -> bool:
        """Refresh only the latest target projection; never rewrite its website snapshot."""

        return self.reproject_candidate_ad_intelligence(candidate_id)

    @staticmethod
    def _detect_changes(
        previous: sqlite3.Row | None,
        observation: SiteObservation,
        score: ScoreCard,
        final_score: int,
    ) -> list[dict[str, Any]]:
        if previous is None:
            return [{"kind": "new_candidate", "summary": "First recorded snapshot"}]
        old_observation = SiteObservation.model_validate_json(previous["observation_json"])
        old_score = ScoreCard.model_validate_json(previous["score_json"])
        changes: list[dict[str, Any]] = []

        def add(kind: str, summary: str, before: Any, after: Any) -> None:
            changes.append({"kind": kind, "summary": summary, "before": before, "after": after})

        if old_observation.live != observation.live:
            add(
                "liveness",
                "Site became live" if observation.live else "Site became unavailable",
                old_observation.live,
                observation.live,
            )
        if old_observation.final_url != observation.final_url:
            add(
                "redirect",
                "Canonical destination changed",
                old_observation.final_url,
                observation.final_url,
            )
        if old_observation.status_code != observation.status_code:
            add(
                "http_status",
                "HTTP status changed",
                old_observation.status_code,
                observation.status_code,
            )
        if (
            old_observation.content_hash
            and observation.content_hash
            and old_observation.content_hash != observation.content_hash
        ):
            add("content", "Inspected page content changed", "previous hash", "new hash")
        old_contacts = sorted(
            contact.address
            for contact in old_observation.contacts.emails
            if contact.outreach_eligible and not contact.historical
        )
        new_contacts = sorted(
            contact.address
            for contact in observation.contacts.emails
            if contact.outreach_eligible and not contact.historical
        )
        if old_contacts != new_contacts:
            add(
                "contacts",
                "Public business email set changed",
                old_contacts,
                new_contacts,
            )
        if old_observation.signals != observation.signals:
            add(
                "signals",
                "Qualification signals changed",
                old_observation.signals,
                observation.signals,
            )
        old_web = old_observation.web
        new_web = observation.web
        if (
            old_web.sitemap_total_urls != new_web.sitemap_total_urls
            or old_web.sitemap_recent_30d != new_web.sitemap_recent_30d
            or old_web.sitemap_counts != new_web.sitemap_counts
        ):
            add(
                "web_velocity",
                "Public product, campaign, case-study, or content footprint changed",
                {
                    "urls": old_web.sitemap_total_urls,
                    "recent_30d": old_web.sitemap_recent_30d,
                    "mix": old_web.sitemap_counts,
                },
                {
                    "urls": new_web.sitemap_total_urls,
                    "recent_30d": new_web.sitemap_recent_30d,
                    "mix": new_web.sitemap_counts,
                },
            )
        if (
            old_web.open_jobs != new_web.open_jobs
            or old_web.creative_jobs != new_web.creative_jobs
            or old_web.growth_jobs != new_web.growth_jobs
        ):
            add(
                "hiring",
                "Public hiring footprint changed",
                {
                    "open": old_web.open_jobs,
                    "creative": old_web.creative_jobs,
                    "growth": old_web.growth_jobs,
                },
                {
                    "open": new_web.open_jobs,
                    "creative": new_web.creative_jobs,
                    "growth": new_web.growth_jobs,
                },
            )
        old_vendors = set(old_web.vendors.vendors)
        new_vendors = set(new_web.vendors.vendors)
        if old_vendors != new_vendors:
            added_vendors = sorted(new_vendors - old_vendors)
            removed_vendors = sorted(old_vendors - new_vendors)
            add(
                "vendor_stack",
                "Public vendor-stack evidence changed",
                {
                    "stack_score": old_web.vendors.stack_score,
                    "vendors": sorted(old_vendors),
                },
                {
                    "stack_score": new_web.vendors.stack_score,
                    "vendors": sorted(new_vendors),
                    "added": added_vendors,
                    "removed": removed_vendors,
                },
            )
        old_sales = old_web.sales
        new_sales = new_web.sales
        sales_changed = (
            set(old_sales.growth_technologies) != set(new_sales.growth_technologies)
            or set(old_sales.creative_technologies) != set(new_sales.creative_technologies)
            or abs(old_sales.image_assets - new_sales.image_assets) >= 5
            or old_sales.video_assets + old_sales.embedded_video_players
            != new_sales.video_assets + new_sales.embedded_video_players
            or old_sales.catalog_total_hint != new_sales.catalog_total_hint
            or abs(old_sales.catalog_median_price - new_sales.catalog_median_price) >= 5
            or old_sales.feed_commercial_items != new_sales.feed_commercial_items
            or old_sales.news_commercial_triggers != new_sales.news_commercial_triggers
            or old_web.vendors.bimi_published != new_web.vendors.bimi_published
        )
        if sales_changed:
            add(
                "sales_osint",
                "Public creative, growth, commerce, or commercial-trigger evidence changed",
                {
                    "overall_score": old_sales.overall_score,
                    "growth_stack": old_sales.growth_technologies,
                    "creative_stack": old_sales.creative_technologies,
                    "media_assets": (
                        old_sales.image_assets
                        + old_sales.video_assets
                        + old_sales.embedded_video_players
                    ),
                    "catalog_total": old_sales.catalog_total_hint,
                    "feed_triggers": old_sales.feed_commercial_items,
                    "news_triggers": old_sales.news_commercial_triggers,
                },
                {
                    "overall_score": new_sales.overall_score,
                    "growth_stack": new_sales.growth_technologies,
                    "creative_stack": new_sales.creative_technologies,
                    "media_assets": (
                        new_sales.image_assets
                        + new_sales.video_assets
                        + new_sales.embedded_video_players
                    ),
                    "catalog_total": new_sales.catalog_total_hint,
                    "feed_triggers": new_sales.feed_commercial_items,
                    "news_triggers": new_sales.news_commercial_triggers,
                },
            )
        if old_score.total != score.total or int(previous["final_score"]) != final_score:
            add(
                "score",
                "Prospect score changed",
                {"base": old_score.total, "final": int(previous["final_score"])},
                {"base": score.total, "final": final_score},
            )
        if old_score.contact_priority != score.contact_priority:
            add(
                "priority",
                "Contact priority changed",
                old_score.contact_priority,
                score.contact_priority,
            )
        return changes

    def latest_run_id(self) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM runs WHERE status IN ('complete', 'partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def sync_registry_sources(self, entries: list[RegistrySource]) -> int:
        entries = [entry for entry in entries if not is_disallowed(entry.apex)]
        timestamp = _now()
        for entry in entries:
            self.connection.execute(
                """
                INSERT INTO managed_sources(
                    apex, category, tier, indexed_names, expected_fit, notes,
                    enabled, registry_managed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(apex) DO UPDATE SET
                    category=excluded.category,
                    tier=excluded.tier,
                    indexed_names=excluded.indexed_names,
                    expected_fit=excluded.expected_fit,
                    notes=excluded.notes,
                    registry_managed=1,
                    updated_at=excluded.updated_at
                """,
                (
                    entry.apex,
                    entry.category,
                    entry.tier,
                    entry.indexed_names,
                    entry.expected_fit,
                    entry.notes,
                    int(entry.recommended or entry.tier == "A"),
                    timestamp,
                    timestamp,
                ),
            )
        self.connection.commit()
        return len(entries)

    def add_managed_source(
        self,
        apex: str,
        *,
        category: str = "Other",
        tier: str = "Research",
        notes: str = "Added from dashboard",
        enabled: bool = True,
        registry_managed: bool = False,
    ) -> None:
        timestamp = _now()
        self.connection.execute(
            """
            INSERT INTO managed_sources(
                apex, category, tier, notes, enabled, registry_managed,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apex) DO UPDATE SET
                category=excluded.category,
                tier=excluded.tier,
                notes=excluded.notes,
                enabled=excluded.enabled,
                registry_managed=MAX(managed_sources.registry_managed, excluded.registry_managed),
                updated_at=excluded.updated_at
            """,
            (
                apex,
                category,
                tier,
                notes,
                int(enabled),
                int(registry_managed),
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()

    def set_source_enabled(self, apex: str, enabled: bool) -> bool:
        cursor = self.connection.execute(
            "UPDATE managed_sources SET enabled=?, updated_at=? WHERE apex=?",
            (int(enabled), _now(), apex),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def get_ai_cache(self, content_key: str) -> AIAnalysis | None:
        row = self.connection.execute(
            "SELECT analysis_json FROM ai_cache WHERE content_key=?", (content_key,)
        ).fetchone()
        if row is None:
            return None
        try:
            analysis = AIAnalysis.model_validate_json(row["analysis_json"])
        except ValueError:
            return None
        self.connection.execute(
            "UPDATE ai_cache SET last_used_at=? WHERE content_key=?", (_now(), content_key)
        )
        self.connection.commit()
        return analysis

    def set_ai_cache(
        self,
        content_key: str,
        analysis: AIAnalysis,
        *,
        model: str,
        web_search: bool,
    ) -> None:
        timestamp = _now()
        self.connection.execute(
            """
            INSERT INTO ai_cache(
                content_key, analysis_json, model, web_search, created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_key) DO UPDATE SET
                analysis_json=excluded.analysis_json,
                last_used_at=excluded.last_used_at
            """,
            (
                content_key,
                analysis.model_dump_json(),
                model,
                int(web_search),
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()

    def get_free_signal_cache(self, host: str, signal_kind: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload_json FROM free_signal_cache
            WHERE host=? AND signal_kind=? AND expires_at>?
            """,
            (host.lower(), signal_kind, _now()),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def set_free_signal_cache(
        self,
        host: str,
        signal_kind: str,
        payload: dict[str, Any],
        ttl_hours: int,
    ) -> None:
        require_allowed(host)
        observed_at = datetime.now(UTC)
        direct_error = bool(str(payload.get("error") or "").strip())
        payload_errors = payload.get("errors", [])
        if not isinstance(payload_errors, list):
            payload_errors = [payload_errors]
        error_count = int(direct_error) + len(
            [error for error in payload_errors if str(error).strip()]
        )
        substantive = bool(
            payload.get("valid")
            or payload.get("checked")
            or payload.get("records")
            or payload.get("evidence")
            or payload.get("articles")
            or payload.get("article_count")
            or payload.get("items")
            or payload.get("total_urls")
            or payload.get("open_jobs")
            or payload.get("sample_size")
        )
        if error_count and not substantive:
            cache_status = "failed"
            effective_ttl_hours = min(max(1, ttl_hours), 1)
        elif error_count:
            cache_status = "partial"
            effective_ttl_hours = min(max(1, ttl_hours), 6)
        else:
            cache_status = "success"
            effective_ttl_hours = max(1, ttl_hours)
        expires_at = observed_at + timedelta(hours=effective_ttl_hours)
        if signal_kind == "provider_rate_limit":
            # Control metadata expires exactly when requests may resume; never evidence.
            expires_at = datetime.fromisoformat(str(payload["retry_after"])).astimezone(UTC)
        self.connection.execute(
            """
            INSERT INTO free_signal_cache(
                host, signal_kind, payload_json, cache_status, error_count,
                observed_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host, signal_kind) DO UPDATE SET
                payload_json=excluded.payload_json,
                cache_status=excluded.cache_status,
                error_count=excluded.error_count,
                observed_at=excluded.observed_at,
                expires_at=excluded.expires_at
            WHERE excluded.signal_kind <> 'provider_rate_limit'
               OR free_signal_cache.expires_at < excluded.expires_at
            """,
            (
                host.lower(),
                signal_kind,
                json.dumps(payload, sort_keys=True),
                cache_status,
                error_count,
                observed_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        self.connection.commit()

    def previous_observation(self, hostname: str) -> SiteObservation | None:
        row = self.connection.execute(
            """
            SELECT s.observation_json
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            WHERE c.hostname=?
            ORDER BY s.id DESC LIMIT 1
            """,
            (hostname,),
        ).fetchone()
        return SiteObservation.model_validate_json(row["observation_json"]) if row else None

    def snapshot_observation(self, snapshot_id: int) -> SiteObservation | None:
        row = self.connection.execute(
            "SELECT observation_json FROM snapshots WHERE id=?", (snapshot_id,),
        ).fetchone()
        return SiteObservation.model_validate_json(row["observation_json"]) if row else None

    def latest_snapshot_planning(self, hostnames: list[str]) -> dict[str, SnapshotPlanning]:
        """Read exact website ordering inputs without loading historical model payloads."""
        planning = {}
        hosts = list(dict.fromkeys(hostnames))
        for start in range(0, len(hosts), 500):
            batch = hosts[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT c.hostname,s.id,s.observed_at,s.final_score,
                       json_extract(s.observation_json, '$.live') AS live,
                       json_extract(s.observation_json, '$.error') AS error,
                       json_extract(s.score_json, '$.budget_ready') AS budget_ready,
                       ti.target_score,ti.principal_signals_met,ti.review_ready,
                       CASE WHEN ti.hypothesis_json IS NULL OR ti.hypothesis_json=''
                            THEN s.score_json END AS legacy_score_json
                FROM candidates c
                JOIN snapshots s ON s.id=(
                    SELECT MAX(newer.id) FROM snapshots newer WHERE newer.candidate_id=c.id
                )
                LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                WHERE c.hostname IN ({placeholders})
                """,
                batch,
            )
            for row in rows:
                if row["legacy_score_json"] is not None:
                    hypothesis = ScoreCard.model_validate_json(
                        row["legacy_score_json"],
                    ).creative_capacity
                    signals_met = sum(
                        signal.truth_state == "met" for _, signal in hypothesis.principal_signals
                    )
                    target_strength = (
                        hypothesis.total,
                        signals_met,
                        hypothesis.review_ready,
                    )
                else:
                    target_strength = (
                        int(row["target_score"]), int(row["principal_signals_met"]),
                        bool(row["review_ready"]),
                    )
                planning[str(row["hostname"])] = SnapshotPlanning(
                    int(row["id"]), str(row["observed_at"]), int(row["final_score"]),
                    bool(row["live"]), bool(row["error"]), bool(row["budget_ready"]),
                    target_strength,
                )
        return planning

    def latest_snapshots(
        self, hostnames: list[str], *, ai_review_only: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Return each host's latest snapshot without exceeding SQLite parameter limits."""
        if not hostnames:
            return {}
        snapshots: dict[str, dict[str, Any]] = {}
        projection = (
            "ti.hypothesis_json AS target_hypothesis_json"
            if ai_review_only else f"s.ai_json, s.final_score, {TARGET_PROJECTION_SELECT}"
        )
        unique_hosts = list(dict.fromkeys(hostnames))
        for start in range(0, len(unique_hosts), 500):
            batch = unique_hosts[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT c.hostname, s.observed_at, s.observation_json, s.score_json,
                       {projection}
                FROM candidates c
                JOIN snapshots s ON s.candidate_id=c.id
                LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                WHERE c.hostname IN ({placeholders})
                  AND s.id = (
                      SELECT MAX(newer.id) FROM snapshots newer
                      WHERE newer.candidate_id=c.id
                  )
                """,
                batch,
            )
            for row in rows:
                score = ScoreCard.model_validate_json(row["score_json"])
                if ai_review_only:
                    if row["target_hypothesis_json"]:
                        score.creative_capacity = CreativeCapacityHypothesis.model_validate_json(
                            row["target_hypothesis_json"]
                        )
                    snapshots[str(row["hostname"])] = {
                        "observation": SiteObservation.model_validate_json(row["observation_json"]),
                        "score": score,
                    }
                    continue
                target_intelligence = self._target_intelligence_from_row(row)
                if target_intelligence is not None:
                    score.creative_capacity = CreativeCapacityHypothesis.model_validate(
                        target_intelligence["hypothesis"]
                    )
                snapshots[str(row["hostname"])] = {
                    "observed_at": row["observed_at"],
                    "observation": SiteObservation.model_validate_json(row["observation_json"]),
                    "score": score,
                    "ai": (
                        AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None
                    ),
                    "final_score": int(row["final_score"]),
                    "target_intelligence": target_intelligence,
                }
        return snapshots

    def recent_snapshots(
        self, hostnames: list[str], *, max_age_hours: int = 24
    ) -> dict[str, dict[str, Any]]:
        """Return only fully completed company work inside the requested age window."""
        normalized_hosts = list(
            dict.fromkeys(
                str(hostname).strip().lower().rstrip(".")
                for hostname in hostnames
                if str(hostname).strip()
            )
        )
        if not normalized_hosts:
            return {}
        candidate_by_host: dict[str, int] = {}
        for start in range(0, len(normalized_hosts), 500):
            batch = normalized_hosts[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"SELECT id, hostname FROM candidates WHERE hostname IN ({placeholders})",
                batch,
            ).fetchall()
            candidate_by_host.update({str(row["hostname"]): int(row["id"]) for row in rows})
        completed = self.completed_company_snapshots(
            list(candidate_by_host.values()),
            max_age_hours=max(1, max_age_hours),
        )
        return {
            hostname: completed[candidate_id]
            for hostname in normalized_hosts
            if (candidate_id := candidate_by_host.get(hostname)) in completed
        }

    def start_scout_run(self, model: str, max_tool_calls: int) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO source_scout_runs(started_at, status, model, max_tool_calls)
            VALUES (?, 'running', ?, ?)
            """,
            (_now(), model, max_tool_calls),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_scout_run(self, run_id: int, status: str, summary: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE source_scout_runs
            SET finished_at=?, status=?, summary_json=?
            WHERE id=?
            """,
            (_now(), status, json.dumps(summary, sort_keys=True), run_id),
        )
        self.connection.commit()

    def recover_running_scout_runs(self) -> int:
        cursor = self.connection.execute(
            """
            UPDATE source_scout_runs
            SET finished_at=?, status='failed',
                summary_json=?
            WHERE status='running' AND model NOT LIKE 'ai-query-source-scout:%'
            """,
            (_now(), json.dumps({"error": "stale_worker_recovered"})),
        )
        self.connection.commit()
        return cursor.rowcount

    def recent_source_scout_query_fingerprints(
        self,
        *,
        max_age_days: int = SOURCE_SCOUT_QUERY_REUSE_DAYS,
        max_runs: int = SOURCE_SCOUT_QUERY_REUSE_RUNS,
        per_family_limit: int = SOURCE_SCOUT_QUERY_REUSE_PER_FAMILY,
    ) -> dict[str, list[str]]:
        """Return bounded successful query fingerprints from the existing run ledger."""

        cutoff = datetime.now(UTC) - timedelta(days=max(1, int(max_age_days)))
        rows = self.connection.execute(
            """
            SELECT status, summary_json
            FROM source_scout_runs
            WHERE status IN ('complete', 'partial')
              AND COALESCE(finished_at, started_at)>=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (cutoff.isoformat(), max(1, min(int(max_runs), 500))),
        ).fetchall()
        bounded = max(1, min(int(per_family_limit), 1_000))
        collected: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        for row in rows:
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except (TypeError, ValueError):
                continue
            family_results = summary.get("family_results", {})
            if not isinstance(family_results, dict):
                continue
            for family, raw_result in family_results.items():
                if not isinstance(family, str) or not isinstance(raw_result, dict):
                    continue
                if raw_result.get("query_reuse_eligible") is not True:
                    continue
                if int(raw_result.get("observed_search_actions") or 0) < 1:
                    continue
                fingerprints = (
                    raw_result.get("executed_query_fingerprints")
                    if "executed_query_fingerprints" in raw_result
                    else raw_result.get("query_variant_fingerprints", [])
                )
                if not isinstance(fingerprints, list):
                    continue
                for fingerprint in fingerprints:
                    normalized = str(fingerprint or "").strip().lower()
                    if not normalized:
                        continue
                    family_items = collected.setdefault(family, [])
                    family_seen = seen.setdefault(family, set())
                    if normalized in family_seen or len(family_items) >= bounded:
                        continue
                    family_seen.add(normalized)
                    family_items.append(normalized)
        return collected

    def resolve_source_suggestion_id(self, suggestion_id: int) -> int | None:
        normalized = int(suggestion_id)
        if normalized <= 0:
            return None
        row = self.connection.execute(
            """
            SELECT COALESCE(duplicate_of_suggestion_id, id) AS canonical_id
            FROM source_suggestions
            WHERE id=?
            """,
            (normalized,),
        ).fetchone()
        return int(row["canonical_id"]) if row is not None else None

    def canonical_source_suggestion_ids(self, suggestion_ids: list[int]) -> list[int]:
        resolved = [
            canonical
            for value in suggestion_ids
            if (canonical := self.resolve_source_suggestion_id(int(value))) is not None
        ]
        return list(dict.fromkeys(resolved))

    def _canonical_source_request(
        self,
        request: dict[str, Any],
        suggestion_ids: list[int],
    ) -> dict[str, Any]:
        normalized = dict(request)
        if "suggestion_ids" in normalized:
            normalized["suggestion_ids"] = suggestion_ids
        return normalized

    def source_scout_due(
        self,
        *,
        max_age_hours: int = 24 * 7,
        pending_limit: int = 12,
    ) -> bool:
        """Return whether Go should spend another bounded source-research cycle."""

        pending = int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM source_suggestions
                WHERE status='pending' AND duplicate_of_suggestion_id IS NULL
                """
            ).fetchone()[0]
        )
        if pending >= max(1, pending_limit):
            return False
        latest = self.connection.execute(
            "SELECT started_at, finished_at, status FROM source_scout_runs "
            "WHERE model<>'tinyfish.web.search' AND model NOT LIKE 'ai-query-source-scout:%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return True
        if latest["status"] == "running":
            return False
        raw_timestamp = latest["finished_at"] or latest["started_at"]
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
        except (TypeError, ValueError):
            return True
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        retry_window = 24 if latest["status"] in {"failed", "partial"} else max(1, max_age_hours)
        return datetime.now(UTC) - timestamp >= timedelta(hours=retry_window)

    @staticmethod
    def _suggestion_score(suggestion: SourceSuggestion) -> int:
        score = {"medium": 55, "high": 72, "very_high": 86}[suggestion.expected_6k_fit]
        score += {"low": -8, "medium": 0, "high": 7}[suggestion.confidence]
        if suggestion.source_kind in {"tenant_apex", "brand_group_portfolio"}:
            score += 4
        score += min(len(suggestion.evidence_urls), 3)
        return max(0, min(score, 100))

    def upsert_source_suggestions(
        self,
        run_id: int,
        suggestions: list[SourceSuggestion],
        *,
        auto_accept_new: bool = False,
    ) -> int:
        # Import locally to keep the store module independent during application startup.
        from .source_discovery import canonicalize_public_source_url

        suggestions = [suggestion for suggestion in suggestions
                       if not is_disallowed(suggestion.url)
                       and not is_disallowed(suggestion.apex or "")]
        timestamp = _now()
        accepted_new = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for suggestion in suggestions:
                # Provider/model payloads sometimes append human annotation to an otherwise
                # usable URL. Normalize only when the shared public-URL policy can recover one
                # unambiguous URL; preserve invalid input for audit when it cannot.
                normalized_url = canonicalize_public_source_url(suggestion.url)
                if normalized_url is not None and normalized_url != suggestion.url:
                    suggestion = suggestion.model_copy(update={"url": normalized_url})
                identity = source_identity_key(
                    source_kind=suggestion.source_kind,
                    apex=suggestion.apex or "",
                    url=suggestion.url,
                    discovery_mechanism=suggestion.discovery_mechanism,
                    legacy_token=suggestion.source_key,
                )
                existing = self.connection.execute(
                    """
                    SELECT id
                    FROM source_suggestions
                    WHERE source_identity_key=? AND duplicate_of_suggestion_id IS NULL
                    LIMIT 1
                    """,
                    (identity,),
                ).fetchone()
                desired_key = (
                    suggestion.source_key or suggestion.apex or suggestion.url.rstrip("/").lower()
                )
                if existing is None:
                    key_match = self.connection.execute(
                        """
                        SELECT COALESCE(duplicate_of_suggestion_id, id) AS canonical_id
                        FROM source_suggestions
                        WHERE source_key=?
                        """,
                        (desired_key,),
                    ).fetchone()
                    if key_match is not None:
                        matched = self.connection.execute(
                            "SELECT id, source_kind, status FROM source_suggestions WHERE id=?",
                            (int(key_match["canonical_id"]),),
                        ).fetchone()
                        distinct_saved_search = (
                            matched is not None
                            and matched["source_kind"] == "repeatable_search"
                            and suggestion.source_kind == "repeatable_search"
                        )
                        if matched is not None and not distinct_saved_search:
                            existing = matched
                            if matched["status"] == "pending":
                                self.connection.execute(
                                    """
                                    UPDATE source_suggestions
                                    SET source_identity_key=?
                                    WHERE id=?
                                    """,
                                    (identity, int(matched["id"])),
                                )
                values = (
                    suggestion.source_kind,
                    suggestion.apex,
                    suggestion.url,
                    suggestion.name,
                    suggestion.category,
                    suggestion.relationship_type,
                    suggestion.source_family,
                    json.dumps(suggestion.buyer_archetypes),
                    suggestion.discovery_mechanism,
                    suggestion.expected_6k_fit,
                    suggestion.confidence,
                    json.dumps(suggestion.rationale),
                    json.dumps(suggestion.evidence_urls),
                    self._suggestion_score(suggestion),
                    run_id,
                    timestamp,
                )
                if existing is not None:
                    self.connection.execute(
                        """
                        UPDATE source_suggestions SET
                            source_kind=CASE WHEN status='pending' THEN ? ELSE source_kind END,
                            apex=CASE WHEN status='pending' THEN ? ELSE apex END,
                            url=CASE WHEN status='pending' THEN ? ELSE url END,
                            name=?, category=?,
                            relationship_type=CASE
                                WHEN status='pending' THEN ? ELSE relationship_type END,
                            source_family=CASE
                                WHEN status='pending' THEN ? ELSE source_family END,
                            buyer_archetypes_json=CASE
                                WHEN status='pending' THEN ? ELSE buyer_archetypes_json END,
                            discovery_mechanism=CASE
                                WHEN status='pending' THEN ? ELSE discovery_mechanism END,
                            expected_6k_fit=?, confidence=?,
                            rationale_json=CASE
                                WHEN status='pending' THEN ? ELSE rationale_json END,
                            evidence_urls_json=CASE
                                WHEN status='pending' THEN ? ELSE evidence_urls_json END,
                            intelligence_score=?, scout_run_id=?, last_seen_at=?
                        WHERE id=?
                        """,
                        (*values, int(existing["id"])),
                    )
                    continue

                source_key = desired_key
                conflict = self.connection.execute(
                    "SELECT source_identity_key FROM source_suggestions WHERE source_key=?",
                    (source_key,),
                ).fetchone()
                if conflict is not None:
                    source_key = f"{suggestion.source_kind}:{desired_key}"
                    conflict = self.connection.execute(
                        "SELECT 1 FROM source_suggestions WHERE source_key=?",
                        (source_key,),
                    ).fetchone()
                    if conflict is not None:
                        identity_suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
                        source_key = f"{source_key}:{identity_suffix}"
                cursor = self.connection.execute(
                    """
                    INSERT INTO source_suggestions(
                        source_key, source_identity_key, duplicate_of_suggestion_id,
                        source_kind, apex, url, name, category,
                        relationship_type, source_family, buyer_archetypes_json,
                        discovery_mechanism, expected_6k_fit, confidence,
                        rationale_json, evidence_urls_json, intelligence_score,
                        status, scout_run_id, first_seen_at, last_seen_at
                    ) VALUES (
                        ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', ?, ?, ?
                    )
                    """,
                    (
                        source_key,
                        identity,
                        suggestion.source_kind,
                        suggestion.apex,
                        suggestion.url,
                        suggestion.name,
                        suggestion.category,
                        suggestion.relationship_type,
                        suggestion.source_family,
                        json.dumps(suggestion.buyer_archetypes),
                        suggestion.discovery_mechanism,
                        suggestion.expected_6k_fit,
                        suggestion.confidence,
                        json.dumps(suggestion.rationale),
                        json.dumps(suggestion.evidence_urls),
                        self._suggestion_score(suggestion),
                        run_id,
                        timestamp,
                        timestamp,
                    ),
                )
                if auto_accept_new:
                    self.connection.execute(
                        "UPDATE source_suggestions SET status='accepted' WHERE id=?",
                        (cursor.lastrowid,),
                    )
                    accepted_new += 1
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return accepted_new if auto_accept_new else len(suggestions)

    @staticmethod
    def _source_suggestion_from_row(row: sqlite3.Row) -> dict[str, Any]:
        suggestion = dict(row)
        suggestion.update(
            {
                "rationale": json.loads(row["rationale_json"] or "[]"),
                "evidence_urls": json.loads(row["evidence_urls_json"] or "[]"),
                "buyer_archetypes": json.loads(row["buyer_archetypes_json"] or "[]"),
            }
        )
        return suggestion

    def list_source_suggestions(
        self,
        status: str = "",
        *,
        include_aliases: bool = False,
        suggestion_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("suggestion.status=?")
            params.append(status)
        if not include_aliases:
            conditions.append("suggestion.duplicate_of_suggestion_id IS NULL")
        if suggestion_ids is not None:
            if not suggestion_ids:
                return []
            conditions.append("suggestion.id IN (" + ",".join("?" for _ in suggestion_ids) + ")")
            params.extend(suggestion_ids)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.connection.execute(
            f"""
            SELECT suggestion.*,
                   COALESCE(suggestion.duplicate_of_suggestion_id, suggestion.id)
                       AS canonical_suggestion_id,
                   (
                       SELECT COUNT(*) - 1
                       FROM source_suggestions member
                       WHERE member.source_identity_key=suggestion.source_identity_key
                   ) AS duplicate_count,
                   EXISTS(
                       SELECT 1
                       FROM source_suggestions member
                       WHERE member.source_identity_key=suggestion.source_identity_key
                         AND member.status<>suggestion.status
                   ) AS review_conflict
            FROM source_suggestions suggestion
            {where}
            ORDER BY CASE suggestion.status
                         WHEN 'pending' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                     suggestion.intelligence_score DESC, suggestion.id DESC
            """,
            params,
        ).fetchall()
        return [self._source_suggestion_from_row(row) for row in rows]

    def source_scout_source_context(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return bounded operational source outcomes for the next scout cycle.

        This is crawlability and discovery-yield feedback only. It deliberately excludes
        company scores, sales qualification, outreach state, and other conversion signals.
        """

        rows = self.connection.execute(
            """
            WITH execution_membership AS (
                SELECT COALESCE(member_source.duplicate_of_suggestion_id,
                                member_source.id) AS canonical_id,
                       execution.id AS execution_run_id,
                       execution.status AS latest_execution_status,
                       execution.pages_fetched,
                       execution.candidates_persisted AS candidates_found,
                       execution.candidates_hydrated,
                       execution.error,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(member_source.duplicate_of_suggestion_id,
                                                 member_source.id)
                           ORDER BY execution.updated_at DESC, execution.id DESC
                       ) AS recency_rank
                FROM source_execution_runs execution
                JOIN json_each(execution.suggestion_ids_json) member
                JOIN source_suggestions member_source
                  ON member_source.id=CAST(member.value AS INTEGER)
            ), latest_execution AS (
                SELECT *
                FROM execution_membership
                WHERE recency_rank=1
            )
            SELECT suggestion.source_key, suggestion.name, suggestion.url,
                   suggestion.source_kind, suggestion.source_family,
                   suggestion.relationship_type,
                   suggestion.buyer_archetypes_json, suggestion.status,
                   suggestion.source_identity_key,
                   COALESCE(execution.latest_execution_status, '')
                       AS latest_execution_status,
                   COALESCE(execution.pages_fetched, 0) AS pages_fetched,
                   COALESCE(execution.candidates_found, 0) AS candidates_found,
                   COALESCE(execution.candidates_hydrated, 0) AS candidates_hydrated,
                   COALESCE(execution.error, '') AS error
            FROM source_suggestions suggestion
            LEFT JOIN latest_execution execution
              ON execution.canonical_id=suggestion.id
            WHERE suggestion.duplicate_of_suggestion_id IS NULL
            ORDER BY suggestion.last_seen_at DESC, suggestion.id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [
            {
                "source_key": str(row["source_key"] or "")[:240],
                "source_identity_key": str(row["source_identity_key"] or "")[:320],
                "name": str(row["name"] or "")[:160],
                "url": str(row["url"] or "")[:500],
                "source_kind": str(row["source_kind"] or "")[:40],
                "source_family": str(row["source_family"] or "other")[:40],
                "relationship_type": str(row["relationship_type"] or "buyer")[:24],
                "buyer_archetypes": json.loads(row["buyer_archetypes_json"] or "[]")[:6],
                "status": str(row["status"] or "")[:24],
                "latest_execution_status": str(row["latest_execution_status"] or "")[:24],
                "pages_fetched": int(row["pages_fetched"] or 0),
                "candidates_found": int(row["candidates_found"] or 0),
                "candidates_hydrated": int(row["candidates_hydrated"] or 0),
                "error": str(row["error"] or "")[:300],
            }
            for row in rows
        ]

    def list_accepted_discovery_sources(
        self, *, suggestion_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return accepted source inventory with execution and hydration lineage."""

        canonical_id = (self.resolve_source_suggestion_id(suggestion_id)
                        if suggestion_id is not None else None)
        if suggestion_id is not None and canonical_id is None:
            return []
        member_filter = (
            "WHERE COALESCE(member_source.duplicate_of_suggestion_id,"
            "member_source.id)=:selected_id"
            if canonical_id is not None else ""
        )
        identity_filter = (
            "WHERE COALESCE(duplicate_of_suggestion_id,id)=:selected_id"
            if canonical_id is not None else ""
        )
        selection_filter = "AND suggestion.id=:selected_id" if canonical_id is not None else ""
        rows = self.connection.execute(
            f"""
            WITH execution_membership AS (
                SELECT COALESCE(member_source.duplicate_of_suggestion_id,
                                member_source.id) AS canonical_id,
                       execution.id AS execution_run_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(member_source.duplicate_of_suggestion_id,
                                                 member_source.id)
                           ORDER BY execution.updated_at DESC, execution.id DESC
                       ) AS recency_rank
                FROM source_execution_runs execution
                JOIN json_each(execution.suggestion_ids_json) member
                JOIN source_suggestions member_source
                  ON member_source.id=CAST(member.value AS INTEGER)
                {member_filter}
            ), lineage_counts AS (
                SELECT COALESCE(member_source.duplicate_of_suggestion_id,
                                member_source.id) AS canonical_id,
                       COUNT(DISTINCT lineage.candidate_id) AS candidates_found,
                       COUNT(DISTINCT CASE
                           WHEN EXISTS(SELECT 1 FROM snapshots snapshot
                               WHERE snapshot.candidate_id=lineage.candidate_id)
                           THEN lineage.candidate_id
                       END) AS candidates_hydrated,
                       MAX(lineage.last_seen_at) AS last_candidate_at
                FROM discovery_source_candidates lineage
                JOIN source_suggestions member_source
                  ON member_source.id=lineage.suggestion_id
                {member_filter}
                GROUP BY COALESCE(member_source.duplicate_of_suggestion_id,
                                  member_source.id)
            ), identity_members AS (
                SELECT COALESCE(duplicate_of_suggestion_id, id) AS canonical_id,
                       COUNT(*) - 1 AS duplicate_count,
                       COUNT(DISTINCT status) > 1 AS review_conflict
                FROM source_suggestions
                {identity_filter}
                GROUP BY COALESCE(duplicate_of_suggestion_id, id)
            )
            SELECT suggestion.*,
                   membership.execution_run_id AS latest_run_id,
                   COALESCE(lineage.candidates_found, 0) AS candidates_found,
                   COALESCE(lineage.candidates_hydrated, 0) AS candidates_hydrated,
                   lineage.last_candidate_at,
                   COALESCE(identity_members.duplicate_count, 0) AS duplicate_count,
                   COALESCE(identity_members.review_conflict, 0) AS review_conflict
            FROM source_suggestions suggestion
            LEFT JOIN execution_membership membership
              ON membership.canonical_id=suggestion.id
             AND membership.recency_rank=1
            LEFT JOIN lineage_counts lineage
              ON lineage.canonical_id=suggestion.id
            LEFT JOIN identity_members ON identity_members.canonical_id=suggestion.id
            WHERE suggestion.status='accepted'
              AND suggestion.duplicate_of_suggestion_id IS NULL
              {selection_filter}
            ORDER BY suggestion.intelligence_score DESC, suggestion.id DESC
            """,
            {"selected_id": canonical_id} if canonical_id is not None else {},
        ).fetchall()
        latest_run_ids = [
            int(row["latest_run_id"]) for row in rows if row["latest_run_id"] is not None
        ]
        latest_runs: dict[int, dict[str, Any]] = {}
        if latest_run_ids:
            placeholders = ", ".join("?" for _ in latest_run_ids)
            run_rows = self.connection.execute(
                f"""
                SELECT execution.*,
                       COUNT(DISTINCT lineage.candidate_id) AS candidate_count
                FROM source_execution_runs execution
                LEFT JOIN discovery_source_candidates lineage
                  ON lineage.execution_run_id=execution.id
                WHERE execution.id IN ({placeholders})
                GROUP BY execution.id
                """,
                latest_run_ids,
            ).fetchall()
            latest_runs = {
                int(run_row["id"]): self._source_execution_from_row(run_row) for run_row in run_rows
            }

        sources: list[dict[str, Any]] = []
        for row in rows:
            latest_run_id = int(row["latest_run_id"]) if row["latest_run_id"] is not None else None
            source = dict(row)
            source.pop("latest_run_id", None)
            source.update(
                {
                    "rationale": json.loads(row["rationale_json"] or "[]"),
                    "evidence_urls": json.loads(row["evidence_urls_json"] or "[]"),
                    "buyer_archetypes": json.loads(row["buyer_archetypes_json"] or "[]"),
                    "candidates_found": int(row["candidates_found"] or 0),
                    "candidates_hydrated": int(row["candidates_hydrated"] or 0),
                    "latest_run": latest_runs.get(latest_run_id),
                }
            )
            sources.append(source)
        return sources

    def source_hydration_backlog(
        self,
        suggestion_id: int,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return source-linked candidates that have never produced a saved snapshot."""

        canonical_id = self.resolve_source_suggestion_id(suggestion_id)
        if canonical_id is None:
            return []
        normalized_limit = max(1, min(int(limit), 500))
        rows = self.connection.execute(
            """
            SELECT candidate.id AS candidate_id, candidate.hostname,
                   MAX(lineage.last_seen_at) AS last_seen_at,
                   MAX(lineage.discovery_rank) AS discovery_rank
            FROM discovery_source_candidates lineage
            JOIN source_suggestions member_source
              ON member_source.id=lineage.suggestion_id
            JOIN candidates candidate ON candidate.id=lineage.candidate_id
            WHERE COALESCE(member_source.duplicate_of_suggestion_id,
                           member_source.id)=?
              AND NOT EXISTS (
                  SELECT 1 FROM snapshots snapshot
                  WHERE snapshot.candidate_id=candidate.id
              )
            GROUP BY candidate.id, candidate.hostname
            ORDER BY discovery_rank DESC, last_seen_at DESC, candidate.id
            LIMIT ?
            """,
            (canonical_id, normalized_limit),
        ).fetchall()
        return [
            {
                "candidate_id": int(row["candidate_id"]),
                "hostname": str(row["hostname"]),
                "last_seen_at": row["last_seen_at"],
                "discovery_rank": float(row["discovery_rank"] or 0),
            }
            for row in rows
        ]

    def update_source_suggestion_status(self, suggestion_id: int, status: str) -> dict | None:
        canonical_id = self.resolve_source_suggestion_id(suggestion_id)
        if canonical_id is None:
            return None
        self.connection.execute(
            "UPDATE source_suggestions SET status=?, last_seen_at=? WHERE id=?",
            (status, _now(), canonical_id),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM source_suggestions WHERE id=?", (canonical_id,)
        ).fetchone()
        return self._source_suggestion_from_row(row) if row else None

    @staticmethod
    def _source_execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
        counters = {name: int(row[name] or 0) for name in SOURCE_EXECUTION_COUNTER_FIELDS}
        keys = set(row.keys())
        return {
            "id": int(row["id"]),
            "job_id": row["job_id"],
            "scan_job_id": row["scan_job_id"],
            "scan_run_id": int(row["scan_run_id"]) if row["scan_run_id"] else None,
            "method": row["method"],
            "source_kind": row["source_kind"],
            "status": row["status"],
            "phase": row["phase"],
            "suggestion_ids": json.loads(row["suggestion_ids_json"] or "[]"),
            "request": json.loads(row["request_json"] or "{}"),
            "summary": json.loads(row["summary_json"] or "{}"),
            **counters,
            "progress": counters,
            "candidate_count": int(row["candidate_count"] or 0)
            if "candidate_count" in keys
            else int(counters["candidates_persisted"]),
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
        }

    def start_source_execution_run(
        self,
        method: str,
        source_kind: str,
        suggestion_ids: list[int],
        request: dict[str, Any],
        job_id: str | None = None,
    ) -> int:
        normalized_method = method.strip()
        normalized_kind = source_kind.strip()
        if not normalized_method or not normalized_kind:
            raise ValueError("Source execution method and source kind are required")
        normalized_suggestion_ids = list(dict.fromkeys(int(value) for value in suggestion_ids))
        if any(value <= 0 for value in normalized_suggestion_ids):
            raise ValueError("Source suggestion ids must be positive integers")
        if any(
            self.resolve_source_suggestion_id(value) is None for value in normalized_suggestion_ids
        ):
            raise ValueError("Source suggestion does not exist")
        normalized_suggestion_ids = self.canonical_source_suggestion_ids(normalized_suggestion_ids)
        normalized_job_id = job_id.strip() if job_id and job_id.strip() else None
        request = self._canonical_source_request(request, normalized_suggestion_ids)
        request_json = json.dumps(request, sort_keys=True)
        suggestion_ids_json = json.dumps(normalized_suggestion_ids)
        if normalized_job_id:
            existing = self.connection.execute(
                "SELECT * FROM source_execution_runs WHERE job_id=?",
                (normalized_job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["method"] != normalized_method
                    or existing["source_kind"] != normalized_kind
                    or existing["suggestion_ids_json"] != suggestion_ids_json
                    or existing["request_json"] != request_json
                ):
                    raise ValueError("Source execution job id is already in use")
                return int(existing["id"])
        timestamp = _now()
        cursor = self.connection.execute(
            """
            INSERT INTO source_execution_runs(
                job_id, method, source_kind, status, phase,
                suggestion_ids_json, request_json, sources_total,
                started_at, updated_at
            ) VALUES (?, ?, ?, 'running', 'starting', ?, ?, ?, ?, ?)
            """,
            (
                normalized_job_id,
                normalized_method,
                normalized_kind,
                suggestion_ids_json,
                request_json,
                len(normalized_suggestion_ids),
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def queue_source_execution_run(
        self,
        method: str,
        source_kind: str,
        suggestion_id: int,
        request: dict[str, Any],
        *,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue one accepted source, reusing its live queued or running work."""

        normalized_method = method.strip()
        normalized_kind = source_kind.strip()
        if not normalized_method or not normalized_kind:
            raise ValueError("Source execution method and source kind are required")
        normalized_suggestion_id = int(suggestion_id)
        if normalized_suggestion_id <= 0:
            raise ValueError("Source suggestion id must be a positive integer")
        if not isinstance(request, dict):
            raise ValueError("Source execution request must be an object")
        normalized_job_id = job_id.strip() if job_id and job_id.strip() else None

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            canonical_id = self.resolve_source_suggestion_id(normalized_suggestion_id)
            if canonical_id is None:
                raise ValueError("Source suggestion does not exist")
            normalized_suggestion_id = canonical_id
            canonical_request = self._canonical_source_request(
                request,
                [normalized_suggestion_id],
            )
            request_json = json.dumps(canonical_request, sort_keys=True)
            suggestion_ids_json = json.dumps([normalized_suggestion_id])
            suggestion = self.connection.execute(
                "SELECT source_kind, status FROM source_suggestions WHERE id=?",
                (normalized_suggestion_id,),
            ).fetchone()
            if suggestion is None:
                raise ValueError("Source suggestion does not exist")
            if suggestion["source_kind"] != normalized_kind:
                raise ValueError("Source execution kind does not match the suggestion")
            if suggestion["status"] != "accepted":
                raise ValueError("Only accepted source suggestions can be queued")

            if normalized_job_id:
                existing_job = self.connection.execute(
                    "SELECT * FROM source_execution_runs WHERE job_id=?",
                    (normalized_job_id,),
                ).fetchone()
                if existing_job is not None:
                    if (
                        existing_job["method"] != normalized_method
                        or existing_job["source_kind"] != normalized_kind
                        or existing_job["suggestion_ids_json"] != suggestion_ids_json
                        or existing_job["request_json"] != request_json
                    ):
                        raise ValueError("Source execution job id is already in use")
                    existing_id = int(existing_job["id"])
                    self.connection.commit()
                    existing = self.get_source_execution_run(existing_id)
                    if existing is None:  # pragma: no cover - protected by the transaction
                        raise RuntimeError("Queued source execution disappeared")
                    return existing

            existing = self.connection.execute(
                """
                SELECT execution.id
                FROM source_execution_runs execution
                WHERE execution.status IN ('queued', 'running')
                  AND json_array_length(execution.suggestion_ids_json)=1
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(execution.suggestion_ids_json) member
                      JOIN source_suggestions member_source
                        ON member_source.id=CAST(member.value AS INTEGER)
                      WHERE COALESCE(member_source.duplicate_of_suggestion_id,
                                     member_source.id)=?
                  )
                ORDER BY execution.id DESC
                LIMIT 1
                """,
                (normalized_suggestion_id,),
            ).fetchone()
            if existing is not None:
                existing_id = int(existing["id"])
                self.connection.commit()
                payload = self.get_source_execution_run(existing_id)
                if payload is None:  # pragma: no cover - protected by the transaction
                    raise RuntimeError("Queued source execution disappeared")
                return payload

            timestamp = _now()
            cursor = self.connection.execute(
                """
                INSERT INTO source_execution_runs(
                    job_id, method, source_kind, status, phase,
                    suggestion_ids_json, request_json, sources_total,
                    started_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 'queued', ?, ?, 1, ?, ?)
                """,
                (
                    normalized_job_id,
                    normalized_method,
                    normalized_kind,
                    suggestion_ids_json,
                    request_json,
                    timestamp,
                    timestamp,
                ),
            )
            run_id = int(cursor.lastrowid)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        payload = self.get_source_execution_run(run_id)
        if payload is None:  # pragma: no cover - protected by the committed insert
            raise RuntimeError("Queued source execution disappeared")
        return payload

    def list_queued_source_execution_runs(
        self,
        method: str = "",
    ) -> list[dict[str, Any]]:
        normalized_method = method.strip()
        method_filter = "AND execution.method=?" if normalized_method else ""
        params: tuple[Any, ...] = (normalized_method,) if normalized_method else ()
        rows = self.connection.execute(
            f"""
            SELECT execution.*,
                   COUNT(DISTINCT lineage.candidate_id) AS candidate_count
            FROM source_execution_runs execution
            LEFT JOIN discovery_source_candidates lineage
              ON lineage.execution_run_id=execution.id
            WHERE execution.status='queued' {method_filter}
            GROUP BY execution.id
            ORDER BY execution.id
            """,
            params,
        ).fetchall()
        return [self._source_execution_from_row(row) for row in rows]

    def claim_queued_source_execution_run(
        self,
        run_id: int,
    ) -> dict[str, Any] | None:
        """Atomically claim one queued source run for a local worker."""

        normalized_run_id = int(run_id)
        timestamp = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE source_execution_runs
                SET status='running', phase='starting', updated_at=?,
                    finished_at=NULL, error='', scan_job_id=NULL, scan_run_id=NULL
                WHERE id=? AND status='queued'
                """,
                (timestamp, normalized_run_id),
            )
            claimed = self.get_source_execution_run(normalized_run_id) if cursor.rowcount else None
            self.connection.commit()
            return claimed
        except Exception:
            self.connection.rollback()
            raise

    def cancel_queued_source_execution_runs(
        self,
        suggestion_id: int,
        *,
        reason: str = "source_not_accepted",
    ) -> int:
        """Cancel queued one-source work when its suggestion is no longer accepted."""

        normalized_suggestion_id = int(suggestion_id)
        if normalized_suggestion_id <= 0:
            raise ValueError("Source suggestion id must be a positive integer")
        canonical_id = self.resolve_source_suggestion_id(normalized_suggestion_id)
        if canonical_id is None:
            return 0
        normalized_reason = reason.strip()[:4_000] or "source_not_accepted"
        timestamp = _now()
        cursor = self.connection.execute(
            """
            UPDATE source_execution_runs
            SET status='cancelled', phase='cancelled', finished_at=?, updated_at=?,
                error=?, summary_json=?
            WHERE status='queued'
              AND json_array_length(suggestion_ids_json)=1
              AND EXISTS (
                  SELECT 1
                  FROM json_each(source_execution_runs.suggestion_ids_json) member
                  JOIN source_suggestions member_source
                    ON member_source.id=CAST(member.value AS INTEGER)
                  WHERE COALESCE(member_source.duplicate_of_suggestion_id,
                                 member_source.id)=?
              )
            """,
            (
                timestamp,
                timestamp,
                normalized_reason,
                json.dumps({"error": normalized_reason}, sort_keys=True),
                canonical_id,
            ),
        )
        self.connection.commit()
        return cursor.rowcount

    def get_source_execution_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT r.*, COUNT(DISTINCT d.candidate_id) AS candidate_count
            FROM source_execution_runs r
            LEFT JOIN discovery_source_candidates d ON d.execution_run_id=r.id
            WHERE r.id=?
            GROUP BY r.id
            """,
            (run_id,),
        ).fetchone()
        return self._source_execution_from_row(row) if row else None

    def update_source_execution_run(
        self,
        run_id: int,
        **progress: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "status",
            "phase",
            "scan_job_id",
            "scan_run_id",
            "error",
            "summary",
            *SOURCE_EXECUTION_COUNTER_FIELDS,
        }
        unknown = sorted(set(progress) - allowed)
        if unknown:
            raise ValueError("Unknown source execution progress fields: " + ", ".join(unknown))
        values: dict[str, Any] = {}
        if "status" in progress:
            status = str(progress["status"] or "").strip()
            if status not in SOURCE_EXECUTION_ACTIVE_STATES | SOURCE_EXECUTION_TERMINAL_STATES:
                raise ValueError(f"Invalid source execution status: {status!r}")
            values["status"] = status
        if "phase" in progress:
            phase = str(progress["phase"] or "").strip()
            if not phase:
                raise ValueError("Source execution phase cannot be blank")
            values["phase"] = phase
        if "scan_job_id" in progress:
            scan_job_id = progress["scan_job_id"]
            values["scan_job_id"] = str(scan_job_id).strip() if scan_job_id else None
        if "scan_run_id" in progress:
            scan_run_id = progress["scan_run_id"]
            values["scan_run_id"] = int(scan_run_id) if scan_run_id is not None else None
        for name in SOURCE_EXECUTION_COUNTER_FIELDS:
            if name not in progress:
                continue
            value = int(progress[name])
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            values[name] = value
        if "error" in progress:
            values["error"] = str(progress["error"] or "")[:4_000]
        if "summary" in progress:
            summary = progress["summary"]
            if not isinstance(summary, dict):
                raise ValueError("Source execution summary must be an object")
            values["summary_json"] = json.dumps(summary, sort_keys=True)
        if not values:
            return self.get_source_execution_run(run_id)
        values["updated_at"] = _now()
        assignments = ", ".join(f"{name}=?" for name in values)
        cursor = self.connection.execute(
            f"UPDATE source_execution_runs SET {assignments} WHERE id=?",
            [*values.values(), run_id],
        )
        self.connection.commit()
        return self.get_source_execution_run(run_id) if cursor.rowcount else None

    def finish_source_execution_run(
        self,
        run_id: int,
        status: str,
        summary: dict[str, Any],
        error: str = "",
    ) -> dict[str, Any] | None:
        normalized_status = status.strip()
        if normalized_status not in SOURCE_EXECUTION_TERMINAL_STATES:
            raise ValueError("Source execution run must finish in a terminal state")
        current = self.get_source_execution_run(run_id)
        if current is None:
            return None
        if current["finished_at"]:
            if current["status"] != normalized_status:
                raise ValueError("Source execution run is already finished")
            return current
        timestamp = _now()
        values: dict[str, Any] = {
            "status": normalized_status,
            "phase": normalized_status,
            "summary_json": json.dumps(summary, sort_keys=True),
            "error": error[:4_000],
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
        for name in SOURCE_EXECUTION_COUNTER_FIELDS:
            value = int(summary.get(name, current[name]))
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            values[name] = value
        if summary.get("scan_job_id") is not None:
            values["scan_job_id"] = str(summary["scan_job_id"]).strip() or None
        if summary.get("scan_run_id") is not None:
            values["scan_run_id"] = int(summary["scan_run_id"])
        assignments = ", ".join(f"{name}=?" for name in values)
        self.connection.execute(
            f"UPDATE source_execution_runs SET {assignments} WHERE id=?",
            [*values.values(), run_id],
        )
        self.connection.commit()
        return self.get_source_execution_run(run_id)

    def list_source_execution_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 200))
        rows = self.connection.execute(
            """
            SELECT r.*, COUNT(DISTINCT d.candidate_id) AS candidate_count
            FROM source_execution_runs r
            LEFT JOIN discovery_source_candidates d ON d.execution_run_id=r.id
            GROUP BY r.id
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
        return [self._source_execution_from_row(row) for row in rows]

    def recover_running_source_execution_runs(self) -> int:
        """Return locally interrupted source work to the durable queue."""

        timestamp = _now()
        placeholders = ", ".join("?" for _ in SOURCE_EXECUTION_RECOVERABLE_STATES)
        cursor = self.connection.execute(
            f"""
            UPDATE source_execution_runs
            SET status='queued', phase='queued', finished_at=NULL, updated_at=?,
                error='', scan_job_id=NULL, scan_run_id=NULL
            WHERE status IN ({placeholders})
            """,
            (
                timestamp,
                *SOURCE_EXECUTION_RECOVERABLE_STATES,
            ),
        )
        self.connection.commit()
        return cursor.rowcount

    def cancel_unfinished_source_execution_method(
        self, method: str, *, reason: str,
        started_after: str | None = None, started_before: str | None = None,
    ) -> int:
        """Cancel unfinished method work while preserving progress, summaries and lineage."""
        timestamp = _now()
        placeholders = ", ".join("?" for _ in SOURCE_EXECUTION_ACTIVE_STATES)
        cursor = self.connection.execute(
            f"""
            UPDATE source_execution_runs
            SET status='cancelled', phase='cancelled', finished_at=?, updated_at=?,
                error=COALESCE(NULLIF(error, ''), ?)
            WHERE method=? AND finished_at IS NULL AND status IN ({placeholders})
              AND started_at >= COALESCE(?, started_at)
              AND started_at <= COALESCE(?, started_at)
            """,
            (timestamp, timestamp, reason, method, *SOURCE_EXECUTION_ACTIVE_STATES,
             started_after, started_before),
        )
        self.connection.commit()
        return cursor.rowcount

    def requeue_source_execution_run(self, run_id: int) -> dict[str, Any] | None:
        """Requeue one active source run when its local worker is shutting down."""

        timestamp = _now()
        placeholders = ", ".join("?" for _ in SOURCE_EXECUTION_RECOVERABLE_STATES)
        cursor = self.connection.execute(
            f"""
            UPDATE source_execution_runs
            SET status='queued', phase='queued', finished_at=NULL, updated_at=?,
                error='', scan_job_id=NULL, scan_run_id=NULL
            WHERE id=? AND status IN ({placeholders})
            """,
            (timestamp, int(run_id), *SOURCE_EXECUTION_RECOVERABLE_STATES),
        )
        self.connection.commit()
        return self.get_source_execution_run(run_id) if cursor.rowcount else None

    def source_execution_payload(self, limit: int = 20) -> dict[str, Any]:
        runs = self.list_source_execution_runs(limit=limit)
        active = next(
            (run for run in runs if run["status"] in SOURCE_EXECUTION_ACTIVE_STATES),
            None,
        )
        return {"active": active, "runs": runs}

    def discovery_scan_recency(self) -> dict[str, str]:
        """Return the latest persisted run timestamp for each Discovery scan method."""

        recency: dict[str, str] = {}
        web_run = self.connection.execute(
            """
            SELECT COALESCE(finished_at, started_at) AS last_scan_at
            FROM runs
            WHERE COALESCE(json_array_length(json_extract(config_json, '$.only_hosts')), 0)=0
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if web_run and web_run["last_scan_at"]:
            recency["web_ct"] = str(web_run["last_scan_at"])

        source_runs = self.connection.execute(
            """
            SELECT method, MAX(COALESCE(finished_at, started_at)) AS last_scan_at
            FROM source_execution_runs
            WHERE status<>'queued'
            GROUP BY method
            """
        ).fetchall()
        for row in source_runs:
            if row["method"] and row["last_scan_at"]:
                recency[str(row["method"])] = str(row["last_scan_at"])

        paid_run = self.connection.execute(
            """
            SELECT COALESCE(finished_at, started_at) AS last_scan_at
            FROM ad_discovery_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if paid_run and paid_run["last_scan_at"]:
            recency["paid_ads"] = str(paid_run["last_scan_at"])

        scout_run = self.connection.execute(
            """
            SELECT COALESCE(finished_at, started_at) AS last_scan_at
            FROM source_scout_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if scout_run and scout_run["last_scan_at"]:
            recency["find_sources"] = str(scout_run["last_scan_at"])
        return recency

    def discovery_method_reliability(
        self,
        method_ids: list[str] | tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        """Return all-time terminal-run reliability for every requested Discovery method."""

        methods = list(
            dict.fromkeys(
                str(method_id).strip()
                for method_id in method_ids
                if str(method_id).strip()
            )
        )
        status_counts: dict[str, dict[str, int]] = {method: {} for method in methods}

        def add(method: str, status: object, count: object) -> None:
            if method not in status_counts:
                return
            normalized_status = str(status or "unknown").strip().lower() or "unknown"
            bucket = status_counts[method]
            bucket[normalized_status] = bucket.get(normalized_status, 0) + int(count or 0)

        if "web_ct" in status_counts:
            rows = self.connection.execute(
                """
                SELECT lower(status) AS status, COUNT(*) AS count
                FROM runs
                WHERE finished_at IS NOT NULL
                  AND COALESCE(json_array_length(json_extract(config_json, '$.only_hosts')), 0)=0
                GROUP BY lower(status)
                """
            ).fetchall()
            for row in rows:
                add("web_ct", row["status"], row["count"])

        source_methods = [
            method
            for method in methods
            if method not in {"web_ct", "paid_ads", "find_sources"}
        ]
        if source_methods:
            placeholders = ", ".join("?" for _ in source_methods)
            rows = self.connection.execute(
                f"""
                SELECT method, lower(status) AS status, COUNT(*) AS count
                FROM source_execution_runs
                WHERE finished_at IS NOT NULL
                  AND method IN ({placeholders})
                GROUP BY method, lower(status)
                """,
                source_methods,
            ).fetchall()
            for row in rows:
                add(str(row["method"]), row["status"], row["count"])

        if "paid_ads" in status_counts:
            rows = self.connection.execute(
                """
                SELECT lower(status) AS status, COUNT(*) AS count
                FROM ad_discovery_runs
                WHERE finished_at IS NOT NULL
                GROUP BY lower(status)
                """
            ).fetchall()
            for row in rows:
                add("paid_ads", row["status"], row["count"])

        if "find_sources" in status_counts:
            rows = self.connection.execute(
                """
                SELECT lower(status) AS status, COUNT(*) AS count
                FROM source_scout_runs
                WHERE finished_at IS NOT NULL
                GROUP BY lower(status)
                """
            ).fetchall()
            for row in rows:
                add("find_sources", row["status"], row["count"])

        reliability: dict[str, dict[str, Any]] = {}
        for method in methods:
            breakdown = dict(sorted(status_counts[method].items()))
            attempts = sum(breakdown.values())
            successes = int(breakdown.get("complete", 0))
            failures = max(0, attempts - successes)
            reliability[method] = {
                "attempt_count": attempts,
                "success_count": successes,
                "failure_count": failures,
                "failure_rate": round(failures / attempts * 100, 1) if attempts else 0.0,
                "status_counts": breakdown,
                "unit": "terminal_runs",
                "window": "all_time",
            }
        return reliability

    def link_source_candidates(
        self,
        run_id: int,
        suggestion_id: int,
        candidates: list[DiscoveredHost],
    ) -> int:
        if not candidates:
            return 0
        run = self.get_source_execution_run(run_id)
        if run is None:
            raise ValueError("Source execution run does not exist")
        normalized_suggestion_id = int(suggestion_id)
        if normalized_suggestion_id <= 0:
            raise ValueError("Source suggestion id must be a positive integer")
        canonical_id = self.resolve_source_suggestion_id(normalized_suggestion_id)
        if canonical_id is None:
            raise ValueError("Source suggestion does not exist")
        normalized_suggestion_id = canonical_id
        run_suggestion_ids = self.canonical_source_suggestion_ids(list(run["suggestion_ids"]))
        if run_suggestion_ids and normalized_suggestion_id not in run_suggestion_ids:
            raise ValueError("Source suggestion is not part of this execution run")

        merged_by_host: dict[str, DiscoveredHost] = {}
        for candidate in candidates:
            current = merged_by_host.get(candidate.hostname)
            if current is None:
                merged_by_host[candidate.hostname] = candidate
                continue
            merged_by_host[candidate.hostname] = current.model_copy(
                update={
                    "first_seen": current.first_seen or candidate.first_seen,
                    "discovery_rank": max(current.discovery_rank, candidate.discovery_rank),
                    "label_hint": current.label_hint or candidate.label_hint,
                    "discovery_evidence_urls": list(
                        dict.fromkeys(
                            [
                                *current.discovery_evidence_urls,
                                *candidate.discovery_evidence_urls,
                            ]
                        )
                    )[:20],
                    "provider_record_ids": list(
                        dict.fromkeys(
                            [
                                *current.provider_record_ids,
                                *candidate.provider_record_ids,
                            ]
                        )
                    )[:20],
                }
            )
        merged_candidates = list(merged_by_host.values())
        self.upsert_derived_candidates(merged_candidates)
        candidate_ids = self.candidate_ids(list(merged_by_host))
        timestamp = _now()
        linked_count = 0
        for candidate in merged_candidates:
            if is_disallowed(candidate.hostname):
                continue
            candidate_id = candidate_ids[candidate.hostname]
            linked_count += 1
            existing = self.connection.execute(
                """
                SELECT evidence_urls_json, provider_record_ids_json
                FROM discovery_source_candidates
                WHERE execution_run_id=? AND suggestion_id=? AND candidate_id=?
                """,
                (run_id, normalized_suggestion_id, candidate_id),
            ).fetchone()
            old_evidence = json.loads(existing["evidence_urls_json"] or "[]") if existing else []
            old_provider_ids = (
                json.loads(existing["provider_record_ids_json"] or "[]") if existing else []
            )
            evidence_urls = list(
                dict.fromkeys([*old_evidence, *candidate.discovery_evidence_urls])
            )[:20]
            provider_record_ids = list(
                dict.fromkeys([*old_provider_ids, *candidate.provider_record_ids])
            )[:20]
            self.connection.execute(
                """
                INSERT INTO discovery_source_candidates(
                    execution_run_id, suggestion_id, candidate_id, source_apex,
                    label_hint, discovery_rank, evidence_urls_json,
                    provider_record_ids_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_run_id, suggestion_id, candidate_id) DO UPDATE SET
                    source_apex=excluded.source_apex,
                    label_hint=CASE
                        WHEN discovery_source_candidates.label_hint=''
                        THEN excluded.label_hint
                        ELSE discovery_source_candidates.label_hint
                    END,
                    discovery_rank=MAX(
                        discovery_source_candidates.discovery_rank,
                        excluded.discovery_rank
                    ),
                    evidence_urls_json=excluded.evidence_urls_json,
                    provider_record_ids_json=excluded.provider_record_ids_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    run_id,
                    normalized_suggestion_id,
                    candidate_id,
                    candidate.source_apex,
                    candidate.label_hint,
                    candidate.discovery_rank,
                    json.dumps(evidence_urls),
                    json.dumps(provider_record_ids),
                    timestamp,
                    timestamp,
                ),
            )
        persisted_count = int(
            self.connection.execute(
                """
                SELECT COUNT(DISTINCT candidate_id)
                FROM discovery_source_candidates
                WHERE execution_run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            UPDATE source_execution_runs
            SET candidates_persisted=?, updated_at=?
            WHERE id=?
            """,
            (persisted_count, timestamp, run_id),
        )
        self.connection.commit()
        return linked_count

    @staticmethod
    def _ad_run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "job_id": row["job_id"],
            "provider": row["provider"],
            "mode": row["mode"],
            "status": row["status"],
            "phase": row["phase"],
            "request": json.loads(row["request_json"] or "{}"),
            "estimate": json.loads(row["estimate_json"] or "{}"),
            "rate_card_version": row["rate_card_version"],
            "approved_cap_microusd": int(row["approved_cap_microusd"] or 0),
            "estimated_cost_microusd": int(row["estimated_cost_microusd"] or 0),
            "actual_cost_microusd": int(row["actual_cost_microusd"] or 0),
            "provider_run_ids": json.loads(row["provider_run_ids_json"] or "[]"),
            "provider_dataset_ids": json.loads(row["provider_dataset_ids_json"] or "[]"),
            "cancel_requested": bool(row["cancel_requested"]),
            "progress": {
                "queries_total": int(row["queries_total"] or 0),
                "queries_completed": int(row["queries_completed"] or 0),
                "advertisers_found": int(row["advertisers_found"] or 0),
                "links_verified": int(row["links_verified"] or 0),
                "candidates_promoted": int(row["candidates_promoted"] or 0),
                "creatives_found": int(row["creatives_found"] or 0),
            },
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "summary": json.loads(row["summary_json"] or "{}"),
        }

    def recover_interrupted_ad_discovery_runs(self) -> int:
        """Park unfinished local paid runs without restarting provider work."""
        timestamp = _now()
        cursor = self.connection.execute(
            """
            UPDATE ad_discovery_runs
            SET status='interrupted', phase='interrupted', finished_at=?, updated_at=?,
                error=CASE WHEN error='' THEN 'local_worker_interrupted' ELSE error END
            WHERE status IN ('queued', 'running', 'cancelling')
            """,
            (timestamp, timestamp),
        )
        self.connection.commit()
        return cursor.rowcount

    def start_ad_discovery_run(
        self,
        *,
        provider: str,
        mode: str,
        request: dict[str, Any],
        estimate: dict[str, Any],
        approved_cap_microusd: int,
        estimated_cost_microusd: int = 0,
        rate_card_version: str = "",
        job_id: str | None = None,
        status: str = "queued",
        phase: str = "queued",
    ) -> int:
        approved_cap = int(approved_cap_microusd)
        estimated_cost = int(estimated_cost_microusd)
        if approved_cap < 0 or estimated_cost < 0:
            raise ValueError("Paid-ad costs must be non-negative integer micro-USD")
        if not provider.strip() or not mode.strip():
            raise ValueError("Paid-ad provider and mode are required")
        timestamp = _now()
        cursor = self.connection.execute(
            """
            INSERT INTO ad_discovery_runs(
                job_id, provider, mode, status, phase, request_json, estimate_json,
                rate_card_version, approved_cap_microusd,
                estimated_cost_microusd, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                provider.strip(),
                mode.strip(),
                status.strip() or "queued",
                phase.strip() or "queued",
                json.dumps(request, sort_keys=True),
                json.dumps(estimate, sort_keys=True),
                rate_card_version.strip(),
                approved_cap,
                estimated_cost,
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_ad_discovery_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        actual_cost_microusd: int | None = None,
        provider_run_ids: list[str] | None = None,
        provider_dataset_ids: list[str] | None = None,
        queries_total: int | None = None,
        queries_completed: int | None = None,
        advertisers_found: int | None = None,
        links_verified: int | None = None,
        candidates_promoted: int | None = None,
        creatives_found: int | None = None,
        cancel_requested: bool | None = None,
        error: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status.strip()
        if phase is not None:
            values["phase"] = phase.strip()
        integer_values = {
            "actual_cost_microusd": actual_cost_microusd,
            "queries_total": queries_total,
            "queries_completed": queries_completed,
            "advertisers_found": advertisers_found,
            "links_verified": links_verified,
            "candidates_promoted": candidates_promoted,
            "creatives_found": creatives_found,
        }
        for name, value in integer_values.items():
            if value is None:
                continue
            normalized = int(value)
            if normalized < 0:
                raise ValueError(f"{name} must be non-negative")
            values[name] = normalized
        if provider_run_ids is not None:
            values["provider_run_ids_json"] = json.dumps(
                list(dict.fromkeys(str(value) for value in provider_run_ids if value))
            )
        if provider_dataset_ids is not None:
            values["provider_dataset_ids_json"] = json.dumps(
                list(dict.fromkeys(str(value) for value in provider_dataset_ids if value))
            )
        if cancel_requested is not None:
            values["cancel_requested"] = int(cancel_requested)
        if error is not None:
            values["error"] = error[:4_000]
        if summary is not None:
            values["summary_json"] = json.dumps(summary, sort_keys=True)
        if not values:
            return self.get_ad_discovery_run(run_id)
        values["updated_at"] = _now()
        assignments = ", ".join(f"{name}=?" for name in values)
        cursor = self.connection.execute(
            f"UPDATE ad_discovery_runs SET {assignments} WHERE id=?",
            [*values.values(), run_id],
        )
        self.connection.commit()
        return self.get_ad_discovery_run(run_id) if cursor.rowcount else None

    def finish_ad_discovery_run(
        self,
        run_id: int,
        status: str,
        *,
        summary: dict[str, Any] | None = None,
        error: str = "",
        actual_cost_microusd: int | None = None,
    ) -> dict[str, Any] | None:
        if status not in AD_DISCOVERY_TERMINAL_STATES:
            raise ValueError("Paid-ad run must finish in a terminal state")
        if actual_cost_microusd is not None and int(actual_cost_microusd) < 0:
            raise ValueError("actual_cost_microusd must be non-negative")
        timestamp = _now()
        cursor = self.connection.execute(
            """
            UPDATE ad_discovery_runs
            SET status=?, phase=?, summary_json=?, error=?,
                actual_cost_microusd=COALESCE(?, actual_cost_microusd),
                finished_at=?, updated_at=?
            WHERE id=?
            """,
            (
                status,
                status,
                json.dumps(summary or {}, sort_keys=True),
                error[:4_000],
                int(actual_cost_microusd) if actual_cost_microusd is not None else None,
                timestamp,
                timestamp,
                run_id,
            ),
        )
        self.connection.commit()
        return self.get_ad_discovery_run(run_id) if cursor.rowcount else None

    def get_ad_discovery_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ad_discovery_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        result = self._ad_run_from_row(row)
        result["queries"] = self.list_ad_discovery_queries(run_id)
        return result

    def list_ad_discovery_runs(
        self,
        *,
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        where = "WHERE status=?" if status else ""
        params: list[Any] = [status] if status else []
        rows = self.connection.execute(
            f"SELECT * FROM ad_discovery_runs {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._ad_run_from_row(row) for row in rows]

    @staticmethod
    def _ad_query_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "run_id": int(row["run_id"]),
            "query_key": row["query_key"],
            "platform": row["platform"],
            "surface": row["surface"],
            "phase": row["phase"],
            "query": row["query"],
            "country": row["country"],
            "input_url": row["input_url"],
            "result_cap": int(row["result_cap"] or 0),
            "result_count": int(row["result_count"] or 0),
            "unique_advertisers": int(row["unique_advertisers"] or 0),
            "estimated_cost_microusd": int(row["estimated_cost_microusd"] or 0),
            "actual_cost_microusd": int(row["actual_cost_microusd"] or 0),
            "status": row["status"],
            "provider_run_id": row["provider_run_id"],
            "provider_dataset_id": row["provider_dataset_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_ad_discovery_query(
        self,
        run_id: int,
        *,
        platform: str,
        surface: str,
        phase: str,
        query: str = "",
        country: str = "",
        input_url: str = "",
        result_cap: int = 0,
        result_count: int = 0,
        unique_advertisers: int = 0,
        estimated_cost_microusd: int = 0,
        actual_cost_microusd: int = 0,
        status: str = "queued",
        provider_run_id: str = "",
        provider_dataset_id: str = "",
        error: str = "",
        query_key: str = "",
    ) -> int:
        if not platform.strip() or not surface.strip() or not phase.strip():
            raise ValueError("Query platform, surface, and phase are required")
        numeric_values = {
            "result_cap": result_cap,
            "result_count": result_count,
            "unique_advertisers": unique_advertisers,
            "estimated_cost_microusd": estimated_cost_microusd,
            "actual_cost_microusd": actual_cost_microusd,
        }
        normalized_numbers = {name: int(value) for name, value in numeric_values.items()}
        if any(value < 0 for value in normalized_numbers.values()):
            raise ValueError("Query counts and costs must be non-negative")
        if not query_key:
            identity = {
                "platform": platform.strip().lower(),
                "surface": surface.strip().lower(),
                "phase": phase.strip().lower(),
                "query": query.strip(),
                "country": country.strip().upper(),
                "input_url": input_url.strip(),
            }
            query_key = hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode("utf-8")
            ).hexdigest()
        timestamp = _now()
        self.connection.execute(
            """
            INSERT INTO ad_discovery_queries(
                run_id, query_key, platform, surface, phase, query, country,
                input_url, result_cap, result_count, unique_advertisers,
                estimated_cost_microusd, actual_cost_microusd, status,
                provider_run_id, provider_dataset_id, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, query_key) DO UPDATE SET
                platform=excluded.platform,
                surface=excluded.surface,
                phase=excluded.phase,
                query=excluded.query,
                country=excluded.country,
                input_url=excluded.input_url,
                result_cap=excluded.result_cap,
                result_count=excluded.result_count,
                unique_advertisers=excluded.unique_advertisers,
                estimated_cost_microusd=excluded.estimated_cost_microusd,
                actual_cost_microusd=excluded.actual_cost_microusd,
                status=excluded.status,
                provider_run_id=excluded.provider_run_id,
                provider_dataset_id=excluded.provider_dataset_id,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                query_key,
                platform.strip().lower(),
                surface.strip().lower(),
                phase.strip().lower(),
                query.strip(),
                country.strip().upper(),
                input_url.strip(),
                normalized_numbers["result_cap"],
                normalized_numbers["result_count"],
                normalized_numbers["unique_advertisers"],
                normalized_numbers["estimated_cost_microusd"],
                normalized_numbers["actual_cost_microusd"],
                status.strip() or "queued",
                provider_run_id.strip(),
                provider_dataset_id.strip(),
                error[:4_000],
                timestamp,
                timestamp,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM ad_discovery_queries WHERE run_id=? AND query_key=?",
            (run_id, query_key),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def list_ad_discovery_queries(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM ad_discovery_queries
            WHERE run_id=? ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        return [self._ad_query_from_row(row) for row in rows]

    def recent_completed_ad_query(
        self,
        *,
        platform: str,
        surface: str,
        phase: str,
        country: str,
        input_url: str,
        minimum_result_cap: int,
        fresh_for: timedelta,
        exclude_run_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Find a fresh successful provider read already held in the local ad ledger."""

        if fresh_for.total_seconds() < 0:
            raise ValueError("fresh_for cannot be negative")
        normalized_cap = max(0, int(minimum_result_cap))
        cutoff = (datetime.now(UTC) - fresh_for).isoformat()
        conditions = [
            "q.platform=?",
            "q.surface=?",
            "q.phase=?",
            "q.country=?",
            "q.input_url=?",
            "q.result_cap>=?",
            "q.status='complete'",
            "q.updated_at>?",
            "r.status IN ('complete', 'partial')",
        ]
        params: list[Any] = [
            platform.strip().lower(),
            surface.strip().lower(),
            phase.strip().lower(),
            country.strip().upper(),
            input_url.strip(),
            normalized_cap,
            cutoff,
        ]
        if exclude_run_id is not None:
            conditions.append("q.run_id<>?")
            params.append(int(exclude_run_id))
        row = self.connection.execute(
            f"""
            SELECT q.*
            FROM ad_discovery_queries q
            JOIN ad_discovery_runs r ON r.id=q.run_id
            WHERE {' AND '.join(conditions)}
            ORDER BY q.updated_at DESC, q.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return self._ad_query_from_row(row) if row is not None else None

    def upsert_advertiser_entity(
        self,
        *,
        platform: str,
        external_id: str,
        display_name: str = "",
        handle: str = "",
        profile_url: str = "",
        linked_instagram_handle: str = "",
        coverage_type: str = "",
        provenance: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> int:
        normalized_platform = platform.strip().lower()
        normalized_external_id = external_id.strip()
        if not normalized_platform or not normalized_external_id:
            raise ValueError("Advertiser platform and external ID are required")
        timestamp = observed_at or _now()
        provenance_json = json.dumps(provenance or {}, sort_keys=True)
        self.connection.execute(
            """
            INSERT INTO advertiser_entities(
                platform, external_id, display_name, handle, profile_url,
                linked_instagram_handle, coverage_type, provenance_json,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                display_name=CASE WHEN excluded.display_name<>''
                    THEN excluded.display_name ELSE advertiser_entities.display_name END,
                handle=CASE WHEN excluded.handle<>''
                    THEN excluded.handle ELSE advertiser_entities.handle END,
                profile_url=CASE WHEN excluded.profile_url<>''
                    THEN excluded.profile_url ELSE advertiser_entities.profile_url END,
                linked_instagram_handle=CASE WHEN excluded.linked_instagram_handle<>''
                    THEN excluded.linked_instagram_handle
                    ELSE advertiser_entities.linked_instagram_handle END,
                coverage_type=CASE WHEN excluded.coverage_type<>''
                    THEN excluded.coverage_type ELSE advertiser_entities.coverage_type END,
                provenance_json=CASE WHEN excluded.provenance_json<>'{}'
                    THEN excluded.provenance_json ELSE advertiser_entities.provenance_json END,
                first_seen_at=MIN(advertiser_entities.first_seen_at, excluded.first_seen_at),
                last_seen_at=MAX(advertiser_entities.last_seen_at, excluded.last_seen_at)
            """,
            (
                normalized_platform,
                normalized_external_id,
                display_name.strip()[:500],
                handle.strip()[:300],
                profile_url.strip()[:2_048],
                linked_instagram_handle.strip()[:300],
                coverage_type.strip()[:100],
                provenance_json,
                timestamp,
                timestamp,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM advertiser_entities
            WHERE platform=? AND external_id=?
            """,
            (normalized_platform, normalized_external_id),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def upsert_ad_creative(
        self,
        advertiser_id: int,
        *,
        platform: str,
        external_id: str,
        creative_group_id: str = "",
        creative_fingerprint: str = "",
        is_active: bool = False,
        started_at: str | None = None,
        ended_at: str | None = None,
        publisher_platforms: list[str] | None = None,
        body: str = "",
        headline: str = "",
        cta: str = "",
        landing_url: str = "",
        landing_host: str = "",
        landing_domain: str = "",
        media_type: str = "",
        source_url: str = "",
        coverage_type: str = "",
        provenance: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> int:
        self._admit_company_hosts([landing_url, landing_host, landing_domain])
        for value in (landing_url, landing_host, landing_domain):
            require_allowed(value)
        normalized_platform = platform.strip().lower()
        normalized_external_id = external_id.strip()
        if not normalized_platform or not normalized_external_id:
            raise ValueError("Creative platform and external ID are required")
        timestamp = observed_at or _now()
        platforms = list(
            dict.fromkeys(
                str(value).strip().lower()
                for value in (publisher_platforms or [])
                if str(value).strip()
            )
        )
        self.connection.execute(
            """
            INSERT INTO ad_creatives(
                advertiser_id, platform, external_id, creative_group_id,
                creative_fingerprint, is_active, started_at, ended_at,
                publisher_platforms_json, body, headline, cta, landing_url,
                landing_host, landing_domain, media_type, source_url,
                coverage_type, provenance_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, advertiser_id, external_id) DO UPDATE SET
                creative_group_id=CASE WHEN excluded.creative_group_id<>''
                    THEN excluded.creative_group_id ELSE ad_creatives.creative_group_id END,
                creative_fingerprint=CASE WHEN excluded.creative_fingerprint<>''
                    THEN excluded.creative_fingerprint
                    ELSE ad_creatives.creative_fingerprint END,
                is_active=excluded.is_active,
                started_at=COALESCE(excluded.started_at, ad_creatives.started_at),
                ended_at=excluded.ended_at,
                publisher_platforms_json=CASE
                    WHEN excluded.publisher_platforms_json<>'[]'
                    THEN excluded.publisher_platforms_json
                    ELSE ad_creatives.publisher_platforms_json END,
                body=CASE WHEN excluded.body<>'' THEN excluded.body ELSE ad_creatives.body END,
                headline=CASE WHEN excluded.headline<>''
                    THEN excluded.headline ELSE ad_creatives.headline END,
                cta=CASE WHEN excluded.cta<>'' THEN excluded.cta ELSE ad_creatives.cta END,
                landing_url=CASE WHEN excluded.landing_url<>''
                    THEN excluded.landing_url ELSE ad_creatives.landing_url END,
                landing_host=CASE WHEN excluded.landing_host<>''
                    THEN excluded.landing_host ELSE ad_creatives.landing_host END,
                landing_domain=CASE WHEN excluded.landing_domain<>''
                    THEN excluded.landing_domain ELSE ad_creatives.landing_domain END,
                media_type=CASE WHEN excluded.media_type<>''
                    THEN excluded.media_type ELSE ad_creatives.media_type END,
                source_url=CASE WHEN excluded.source_url<>''
                    THEN excluded.source_url ELSE ad_creatives.source_url END,
                coverage_type=CASE WHEN excluded.coverage_type<>''
                    THEN excluded.coverage_type ELSE ad_creatives.coverage_type END,
                provenance_json=CASE WHEN excluded.provenance_json<>'{}'
                    THEN excluded.provenance_json ELSE ad_creatives.provenance_json END,
                first_seen_at=MIN(ad_creatives.first_seen_at, excluded.first_seen_at),
                last_seen_at=MAX(ad_creatives.last_seen_at, excluded.last_seen_at)
            """,
            (
                advertiser_id,
                normalized_platform,
                normalized_external_id,
                creative_group_id.strip()[:500],
                creative_fingerprint.strip()[:500],
                int(is_active),
                started_at,
                ended_at,
                json.dumps(platforms),
                body[:20_000],
                headline[:2_000],
                cta[:500],
                landing_url.strip()[:4_000],
                landing_host.strip().lower().rstrip(".")[:500],
                landing_domain.strip().lower().rstrip(".")[:500],
                media_type.strip().lower()[:100],
                source_url.strip()[:4_000],
                coverage_type.strip()[:100],
                json.dumps(provenance or {}, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM ad_creatives
            WHERE platform=? AND advertiser_id=? AND external_id=?
            """,
            (normalized_platform, advertiser_id, normalized_external_id),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def record_ad_run_item(
        self,
        run_id: int,
        query_id: int,
        *,
        advertiser_id: int | None = None,
        creative_id: int | None = None,
        provider_item_id: str = "",
        item_key: str = "",
        observed_at: str | None = None,
        active: bool | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        query = self.connection.execute(
            "SELECT run_id FROM ad_discovery_queries WHERE id=?", (query_id,)
        ).fetchone()
        if query is None or int(query["run_id"]) != int(run_id):
            raise ValueError("Paid-ad query does not belong to the run")
        payload_json = json.dumps(payload or {}, sort_keys=True)
        if not item_key:
            if provider_item_id:
                item_key = f"provider:{provider_item_id.strip()}"
            elif creative_id is not None:
                item_key = f"creative:{creative_id}"
            else:
                item_key = "payload:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        timestamp = observed_at or _now()
        self.connection.execute(
            """
            INSERT INTO ad_run_items(
                run_id, query_id, advertiser_id, creative_id, item_key,
                provider_item_id, observed_at, active, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, query_id, item_key) DO UPDATE SET
                advertiser_id=COALESCE(excluded.advertiser_id, ad_run_items.advertiser_id),
                creative_id=COALESCE(excluded.creative_id, ad_run_items.creative_id),
                provider_item_id=CASE WHEN excluded.provider_item_id<>''
                    THEN excluded.provider_item_id ELSE ad_run_items.provider_item_id END,
                observed_at=excluded.observed_at,
                active=COALESCE(excluded.active, ad_run_items.active),
                payload_json=CASE WHEN excluded.payload_json<>'{}'
                    THEN excluded.payload_json ELSE ad_run_items.payload_json END
            """,
            (
                run_id,
                query_id,
                advertiser_id,
                creative_id,
                item_key[:500],
                provider_item_id.strip()[:500],
                timestamp,
                int(active) if active is not None else None,
                payload_json,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM ad_run_items
            WHERE run_id=? AND query_id=? AND item_key=?
            """,
            (run_id, query_id, item_key[:500]),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def upsert_advertiser_domain_link(
        self,
        advertiser_id: int,
        *,
        domain: str,
        candidate_id: int | None = None,
        landing_host: str = "",
        landing_url: str = "",
        state: str = "probable",
        confidence: int = 0,
        landing_domain_match: bool = False,
        social_handle_match: bool = False,
        name_match: bool = False,
        evidence: list[dict[str, Any] | str] | None = None,
        observed_at: str | None = None,
        verified_at: str | None = None,
    ) -> int:
        self._admit_company_hosts([domain, landing_host, landing_url])
        for value in (domain, landing_host, landing_url):
            require_allowed(value)
        normalized_domain = self._normalize_identity_host(domain)
        if not normalized_domain:
            raise ValueError("A public advertiser domain is required")
        normalized_state = state.strip().lower()
        if normalized_state not in AD_DOMAIN_LINK_STATES:
            raise ValueError("Unknown advertiser-domain link state")
        normalized_confidence = int(confidence)
        if not 0 <= normalized_confidence <= 100:
            raise ValueError("Advertiser-domain confidence must be between 0 and 100")
        existing = self.connection.execute(
            """
            SELECT evidence_json FROM advertiser_domain_links
            WHERE advertiser_id=? AND domain=?
            """,
            (advertiser_id, normalized_domain),
        ).fetchone()
        prior_evidence = json.loads(existing["evidence_json"] or "[]") if existing else []
        merged_evidence: list[dict[str, Any] | str] = []
        seen_evidence: set[str] = set()
        for item in [*prior_evidence, *(evidence or [])]:
            key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            merged_evidence.append(item)
            if len(merged_evidence) >= 50:
                break
        timestamp = observed_at or _now()
        link_verified_at = (
            verified_at or timestamp if normalized_state == "verified" else verified_at
        )
        self.connection.execute(
            """
            INSERT INTO advertiser_domain_links(
                advertiser_id, candidate_id, domain, landing_host, landing_url,
                state, confidence, landing_domain_match, social_handle_match,
                name_match, evidence_json, first_seen_at, last_seen_at, verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(advertiser_id, domain) DO UPDATE SET
                candidate_id=CASE
                    WHEN advertiser_domain_links.reviewed_at IS NOT NULL
                    THEN advertiser_domain_links.candidate_id
                    ELSE COALESCE(excluded.candidate_id,
                                  advertiser_domain_links.candidate_id) END,
                landing_host=CASE WHEN excluded.landing_host<>''
                    THEN excluded.landing_host ELSE advertiser_domain_links.landing_host END,
                landing_url=CASE WHEN excluded.landing_url<>''
                    THEN excluded.landing_url ELSE advertiser_domain_links.landing_url END,
                state=CASE
                    WHEN advertiser_domain_links.reviewed_at IS NOT NULL
                    THEN advertiser_domain_links.state
                    WHEN advertiser_domain_links.state='verified'
                         AND excluded.state<>'verified'
                    THEN advertiser_domain_links.state
                    ELSE excluded.state END,
                confidence=CASE
                    WHEN advertiser_domain_links.reviewed_at IS NOT NULL
                    THEN advertiser_domain_links.confidence
                    ELSE MAX(advertiser_domain_links.confidence, excluded.confidence) END,
                landing_domain_match=MAX(
                    advertiser_domain_links.landing_domain_match,
                    excluded.landing_domain_match
                ),
                social_handle_match=MAX(
                    advertiser_domain_links.social_handle_match,
                    excluded.social_handle_match
                ),
                name_match=MAX(advertiser_domain_links.name_match, excluded.name_match),
                evidence_json=excluded.evidence_json,
                first_seen_at=MIN(
                    advertiser_domain_links.first_seen_at, excluded.first_seen_at
                ),
                last_seen_at=MAX(
                    advertiser_domain_links.last_seen_at, excluded.last_seen_at
                ),
                verified_at=COALESCE(
                    advertiser_domain_links.verified_at, excluded.verified_at
                )
            """,
            (
                advertiser_id,
                candidate_id,
                normalized_domain,
                self._normalize_identity_host(landing_host),
                landing_url.strip()[:4_000],
                normalized_state,
                normalized_confidence,
                int(landing_domain_match),
                int(social_handle_match),
                int(name_match),
                json.dumps(merged_evidence, sort_keys=True),
                timestamp,
                timestamp,
                link_verified_at,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM advertiser_domain_links
            WHERE advertiser_id=? AND domain=?
            """,
            (advertiser_id, normalized_domain),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    @staticmethod
    def _advertiser_domain_link_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "advertiser_id": int(row["advertiser_id"]),
            "candidate_id": (int(row["candidate_id"]) if row["candidate_id"] is not None else None),
            "domain": row["domain"],
            "landing_host": row["landing_host"],
            "landing_url": row["landing_url"],
            "state": row["state"],
            "confidence": int(row["confidence"]),
            "match_features": {
                "landing_domain": bool(row["landing_domain_match"]),
                "social_handle": bool(row["social_handle_match"]),
                "name": bool(row["name_match"]),
            },
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "verified_at": row["verified_at"],
            "reviewed_at": row["reviewed_at"],
            "reviewed_by": row["reviewed_by"],
            "review_note": row["review_note"],
            "advertiser": {
                "platform": row["advertiser_platform"],
                "external_id": row["advertiser_external_id"],
                "display_name": row["advertiser_display_name"],
                "handle": row["advertiser_handle"],
                "profile_url": row["advertiser_profile_url"],
                "linked_instagram_handle": row["advertiser_instagram_handle"],
                "coverage_type": row["advertiser_coverage_type"],
            },
        }

    def list_advertiser_domain_links(
        self,
        *,
        state: str = "",
        platform: str = "",
        candidate_id: int | None = None,
        unlinked: bool | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if state:
            conditions.append("l.state=?")
            params.append(state.strip().lower())
        if platform:
            conditions.append("a.platform=?")
            params.append(platform.strip().lower())
        if candidate_id is not None:
            conditions.append("l.candidate_id=?")
            params.append(candidate_id)
        if unlinked is not None:
            conditions.append(
                "l.candidate_id IS NULL" if unlinked else "l.candidate_id IS NOT NULL"
            )
        if search:
            needle = f"%{search.strip().lower()}%"
            conditions.append(
                "(lower(l.domain) LIKE ? OR lower(a.display_name) LIKE ? "
                "OR lower(a.handle) LIKE ? OR lower(a.external_id) LIKE ?)"
            )
            params.extend([needle, needle, needle, needle])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        joins = """
            FROM advertiser_domain_links l
            JOIN advertiser_entities a ON a.id=l.advertiser_id
        """
        total = int(
            self.connection.execute(f"SELECT COUNT(*) {joins} {where}", params).fetchone()[0]
        )
        limit = max(1, min(int(limit), 1_000))
        offset = max(0, int(offset))
        rows = self.connection.execute(
            f"""
            SELECT l.*,
                   a.platform AS advertiser_platform,
                   a.external_id AS advertiser_external_id,
                   a.display_name AS advertiser_display_name,
                   a.handle AS advertiser_handle,
                   a.profile_url AS advertiser_profile_url,
                   a.linked_instagram_handle AS advertiser_instagram_handle,
                   a.coverage_type AS advertiser_coverage_type
            {joins}
            {where}
            ORDER BY CASE l.state
                         WHEN 'ambiguous' THEN 0
                         WHEN 'probable' THEN 1
                         WHEN 'verified' THEN 2
                         ELSE 3 END,
                     l.confidence DESC, l.last_seen_at DESC, l.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return {
            "items": [self._advertiser_domain_link_from_row(row) for row in rows],
            "total": total,
        }

    def get_advertiser_domain_link(self, link_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT l.*,
                   a.platform AS advertiser_platform,
                   a.external_id AS advertiser_external_id,
                   a.display_name AS advertiser_display_name,
                   a.handle AS advertiser_handle,
                   a.profile_url AS advertiser_profile_url,
                   a.linked_instagram_handle AS advertiser_instagram_handle,
                   a.coverage_type AS advertiser_coverage_type
            FROM advertiser_domain_links l
            JOIN advertiser_entities a ON a.id=l.advertiser_id
            WHERE l.id=?
            """,
            (link_id,),
        ).fetchone()
        return self._advertiser_domain_link_from_row(row) if row else None

    def advertiser_creatives(
        self,
        advertiser_id: int,
        *,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["advertiser_id=?"]
        params: list[Any] = [advertiser_id]
        if active_only:
            conditions.append("is_active=1")
        rows = self.connection.execute(
            f"""
            SELECT * FROM ad_creatives
            WHERE {" AND ".join(conditions)}
            ORDER BY is_active DESC, started_at DESC, last_seen_at DESC, id DESC
            LIMIT ?
            """,
            [*params, max(1, min(int(limit), 2_000))],
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "advertiser_id": int(row["advertiser_id"]),
                "platform": row["platform"],
                "external_id": row["external_id"],
                "creative_group_id": row["creative_group_id"],
                "creative_fingerprint": row["creative_fingerprint"],
                "is_active": bool(row["is_active"]),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "publisher_platforms": json.loads(row["publisher_platforms_json"] or "[]"),
                "body": row["body"],
                "headline": row["headline"],
                "cta": row["cta"],
                "landing_url": row["landing_url"],
                "landing_host": row["landing_host"],
                "landing_domain": row["landing_domain"],
                "media_type": row["media_type"],
                "source_url": row["source_url"],
                "coverage_type": row["coverage_type"],
                "provenance": json.loads(row["provenance_json"] or "{}"),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]

    def review_advertiser_domain_link(
        self,
        link_id: int,
        *,
        state: str,
        reviewer: str = "",
        note: str = "",
        confidence: int | None = None,
        candidate_id: int | None = None,
    ) -> dict[str, Any] | None:
        normalized_state = state.strip().lower()
        if normalized_state not in AD_DOMAIN_LINK_STATES:
            raise ValueError("Unknown advertiser-domain link state")
        if confidence is not None and not 0 <= int(confidence) <= 100:
            raise ValueError("Advertiser-domain confidence must be between 0 and 100")
        timestamp = _now()
        self.connection.execute(
            """
            UPDATE advertiser_domain_links
            SET state=?,
                confidence=COALESCE(?, confidence),
                candidate_id=CASE
                    WHEN ?='rejected' THEN NULL
                    ELSE COALESCE(?, candidate_id) END,
                verified_at=CASE WHEN ?='verified' THEN ? ELSE verified_at END,
                reviewed_at=?, reviewed_by=?, review_note=?
            WHERE id=?
            """,
            (
                normalized_state,
                int(confidence) if confidence is not None else None,
                normalized_state,
                candidate_id,
                normalized_state,
                timestamp,
                timestamp,
                reviewer.strip()[:200],
                note.strip()[:2_000],
                link_id,
            ),
        )
        self.connection.commit()
        return self.get_advertiser_domain_link(link_id)

    def record_candidate_ad_observation(
        self,
        candidate_id: int,
        run_id: int,
        *,
        platform: str,
        surface: str,
        coverage_type: str = "",
        identity_state: str = "unresolved",
        identity_confidence: int = 0,
        total_ads: int = 0,
        active_ads: int = 0,
        unique_creatives: int = 0,
        active_unique_creatives: int = 0,
        video_creatives: int = 0,
        image_creatives: int = 0,
        new_creatives_7d: int = 0,
        new_creatives_30d: int = 0,
        new_creatives_90d: int = 0,
        first_started_at: str | None = None,
        latest_started_at: str | None = None,
        platforms: list[str] | None = None,
        publisher_platforms: list[str] | None = None,
        creative_group_ids: list[str] | None = None,
        active_creative_group_ids: list[str] | None = None,
        source_urls: list[str] | None = None,
        coverage_notes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> int:
        normalized_platform = platform.strip().lower()
        normalized_surface = surface.strip().lower()
        if not normalized_platform or not normalized_surface:
            raise ValueError("Candidate ad-observation platform and surface are required")
        normalized_confidence = int(identity_confidence)
        if not 0 <= normalized_confidence <= 100:
            raise ValueError("Ad identity confidence must be between 0 and 100")
        normalized_identity_state = identity_state.strip().lower() or "unresolved"
        if normalized_identity_state == "unknown":
            normalized_identity_state = "unresolved"
        if normalized_identity_state not in AD_IDENTITY_STATES:
            raise ValueError("Unknown ad identity state")
        counts = {
            "total_ads": int(total_ads),
            "active_ads": int(active_ads),
            "unique_creatives": int(unique_creatives),
            "active_unique_creatives": int(active_unique_creatives),
            "video_creatives": int(video_creatives),
            "image_creatives": int(image_creatives),
            "new_creatives_7d": int(new_creatives_7d),
            "new_creatives_30d": int(new_creatives_30d),
            "new_creatives_90d": int(new_creatives_90d),
        }
        if any(value < 0 for value in counts.values()):
            raise ValueError("Candidate ad-observation counts must be non-negative")

        def normalized_list(values: list[str] | None, *, lower: bool = False) -> list[str]:
            cleaned = []
            for raw in values or []:
                value = str(raw).strip()
                if lower:
                    value = value.lower()
                if value and value not in cleaned:
                    cleaned.append(value)
            return cleaned

        observed_platforms = normalized_list(
            [*(platforms or []), *(publisher_platforms or [])], lower=True
        )
        group_ids = normalized_list(creative_group_ids)
        active_group_ids = normalized_list(active_creative_group_ids)
        evidence_urls = normalized_list(source_urls)
        notes = normalized_list(coverage_notes)
        timestamp = observed_at or _now()
        self.connection.execute(
            """
            INSERT INTO candidate_ad_observations(
                candidate_id, run_id, platform, surface, coverage_type,
                identity_state, identity_confidence, active_ad_count,
                total_ad_count, active_creative_groups, total_creative_groups,
                new_creatives_7d, new_creatives_30d, new_creatives_90d,
                video_creatives, image_creatives, first_started_at,
                latest_started_at, publisher_platforms_json,
                creative_group_ids_json, active_creative_group_ids_json,
                evidence_urls_json, coverage_notes_json, metadata_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, run_id, platform, surface) DO UPDATE SET
                coverage_type=excluded.coverage_type,
                identity_state=excluded.identity_state,
                identity_confidence=excluded.identity_confidence,
                active_ad_count=excluded.active_ad_count,
                total_ad_count=excluded.total_ad_count,
                active_creative_groups=excluded.active_creative_groups,
                total_creative_groups=excluded.total_creative_groups,
                new_creatives_7d=excluded.new_creatives_7d,
                new_creatives_30d=excluded.new_creatives_30d,
                new_creatives_90d=excluded.new_creatives_90d,
                video_creatives=excluded.video_creatives,
                image_creatives=excluded.image_creatives,
                first_started_at=excluded.first_started_at,
                latest_started_at=excluded.latest_started_at,
                publisher_platforms_json=excluded.publisher_platforms_json,
                creative_group_ids_json=excluded.creative_group_ids_json,
                active_creative_group_ids_json=excluded.active_creative_group_ids_json,
                evidence_urls_json=excluded.evidence_urls_json,
                coverage_notes_json=excluded.coverage_notes_json,
                metadata_json=excluded.metadata_json,
                observed_at=excluded.observed_at
            """,
            (
                candidate_id,
                run_id,
                normalized_platform,
                normalized_surface,
                coverage_type.strip().lower()[:100],
                normalized_identity_state,
                normalized_confidence,
                counts["active_ads"],
                counts["total_ads"],
                counts["active_unique_creatives"],
                counts["unique_creatives"],
                counts["new_creatives_7d"],
                counts["new_creatives_30d"],
                counts["new_creatives_90d"],
                counts["video_creatives"],
                counts["image_creatives"],
                first_started_at,
                latest_started_at,
                json.dumps(observed_platforms),
                json.dumps(group_ids),
                json.dumps(active_group_ids),
                json.dumps(evidence_urls),
                json.dumps(notes),
                json.dumps(metadata or {}, sort_keys=True),
                timestamp,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM candidate_ad_observations
            WHERE candidate_id=? AND run_id=? AND platform=? AND surface=?
            """,
            (candidate_id, run_id, normalized_platform, normalized_surface),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def current_candidate_ad_intelligence(
        self, candidate_id: int, *, company_key: str | None = None,
        reference_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Return current verified ads at the company's current identity and clock."""
        now = self._utc_datetime(reference_time or datetime.now(UTC)).isoformat()
        if company_key is None:
            identity = self.connection.execute(
                "SELECT canonical_domain FROM snapshot_target_intelligence WHERE candidate_id=? "
                "ORDER BY snapshot_id DESC LIMIT 1", (candidate_id,)).fetchone()
            company_key = identity[0] if identity else None
        aliases = [candidate_id]
        if company_key:
            aliases.extend(r[0] for r in self.connection.execute(
                "SELECT candidate_id FROM engine_ranking_members WHERE company_key=?",
                (company_key,)))
        aliases = sorted(set(aliases))
        placeholders = ",".join("?" for _ in aliases)
        current_query = (
            f"""
            WITH ranked AS (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.platform, o.surface
                           ORDER BY o.observed_at DESC, o.id DESC
                       ) AS recency_rank
                FROM candidate_ad_observations o
                JOIN ad_discovery_runs r ON r.id=o.run_id
                WHERE o.candidate_id IN ({placeholders}) AND o.observed_at<=?
                  AND r.status IN ('complete', 'partial')
            )
            SELECT * FROM ranked WHERE recency_rank=1
            ORDER BY platform, surface
            """,
            (*aliases, now),
        )
        links_query = (
            f"""
            SELECT l.candidate_id,l.confidence, l.evidence_json,
                   a.platform, a.external_id, a.display_name, a.handle,
                   a.profile_url, a.linked_instagram_handle, a.coverage_type
            FROM advertiser_domain_links l
            JOIN advertiser_entities a ON a.id=l.advertiser_id
            WHERE l.candidate_id IN ({placeholders}) AND l.state='verified' AND l.confidence>=80
              AND (? IS NULL OR l.domain=?)
              AND COALESCE(l.verified_at,l.first_seen_at)<=?
            ORDER BY l.confidence DESC, a.platform, a.external_id
            """,
            (*aliases, company_key, company_key, now),
        )
        sources_query = (
            f"""
            SELECT DISTINCT c.source_url
            FROM advertiser_domain_links l
            JOIN ad_creatives c ON c.advertiser_id=l.advertiser_id
            WHERE l.candidate_id IN ({placeholders}) AND l.state='verified' AND l.confidence>=80
              AND (? IS NULL OR l.domain=?) AND c.last_seen_at<=? AND c.source_url<>''
            ORDER BY c.last_seen_at DESC LIMIT 30
            """,
            (*aliases, company_key, company_key, now),
        )
        prefetch = getattr(self.connection, "prefetch", None)
        if prefetch is not None:
            prefetch((current_query, links_query, sources_query))
        current_rows = self.connection.execute(*current_query).fetchall()
        link_rows = self.connection.execute(*links_query).fetchall()
        verified_rows = (
            [
                row
                for row in current_rows
                if row["identity_state"] == "verified" and int(row["identity_confidence"]) >= 80
                and row["candidate_id"] in {link["candidate_id"] for link in link_rows}
            ]
            if link_rows
            else []
        )
        all_confidences = [int(row["identity_confidence"]) for row in current_rows]
        all_confidences.extend(int(row["confidence"]) for row in link_rows)
        identity_confidence = max(all_confidences, default=0)
        if verified_rows or link_rows:
            identity_state = "verified"
        elif current_rows:
            state_priority = {
                "probable": 3,
                "ambiguous": 2,
                "rejected": 1,
                "unresolved": 0,
                "unknown": 0,
            }
            identity_state = max(
                (
                    "unresolved"
                    if str(row["identity_state"]) == "unknown"
                    else "unresolved"
                    if str(row["identity_state"]) == "verified" and not link_rows
                    else str(row["identity_state"])
                    for row in current_rows
                ),
                key=lambda value: state_priority.get(value, 0),
            )
        else:
            identity_state = "unresolved"

        advertiser_ids = list(dict.fromkeys(str(row["external_id"]) for row in link_rows))
        advertiser_names = list(
            dict.fromkeys(str(row["display_name"]) for row in link_rows if row["display_name"])
        )
        meta_row = next(
            (row for row in link_rows if str(row["platform"]) == "meta"),
            None,
        )
        platform_values: list[str] = []
        creative_group_ids: set[str] = set()
        active_creative_group_ids: set[str] = set()
        unkeyed_creatives = 0
        unkeyed_active_creatives = 0
        scoreable_platform_values: list[str] = []
        scoreable_group_ids: set[str] = set()
        scoreable_active_group_ids: set[str] = set()
        scoreable_unkeyed_creatives = 0
        scoreable_unkeyed_active_creatives = 0
        source_urls: list[str] = []
        coverage_notes: list[str] = []
        for row in verified_rows:
            scoreable_row = (
                str(row["surface"]) != "tiktok_top"
                and str(row["coverage_type"]) != "curated_top_ads_positive_only"
            )
            source_platform = str(row["platform"])
            if source_platform and source_platform not in platform_values:
                platform_values.append(source_platform)
            if (
                scoreable_row
                and source_platform
                and source_platform not in scoreable_platform_values
            ):
                scoreable_platform_values.append(source_platform)
            for value in json.loads(row["publisher_platforms_json"] or "[]"):
                if value and value not in platform_values:
                    platform_values.append(value)
                if scoreable_row and value and value not in scoreable_platform_values:
                    scoreable_platform_values.append(value)
            row_groups = json.loads(row["creative_group_ids_json"] or "[]")
            row_active_groups = json.loads(row["active_creative_group_ids_json"] or "[]")
            if row_groups:
                creative_group_ids.update(str(value) for value in row_groups)
                if scoreable_row:
                    scoreable_group_ids.update(str(value) for value in row_groups)
            else:
                unkeyed_creatives += int(row["total_creative_groups"] or 0)
                if scoreable_row:
                    scoreable_unkeyed_creatives += int(row["total_creative_groups"] or 0)
            if row_active_groups:
                active_creative_group_ids.update(str(value) for value in row_active_groups)
                if scoreable_row:
                    scoreable_active_group_ids.update(str(value) for value in row_active_groups)
            else:
                unkeyed_active_creatives += int(row["active_creative_groups"] or 0)
                if scoreable_row:
                    scoreable_unkeyed_active_creatives += int(row["active_creative_groups"] or 0)
            for value in json.loads(row["evidence_urls_json"] or "[]"):
                if value and value not in source_urls:
                    source_urls.append(value)
            for value in json.loads(row["coverage_notes_json"] or "[]"):
                if value and value not in coverage_notes:
                    coverage_notes.append(value)
        for row in link_rows:
            if row["profile_url"] and row["profile_url"] not in source_urls:
                source_urls.append(str(row["profile_url"]))
            for value in json.loads(row["evidence_json"] or "[]"):
                if isinstance(value, str) and value not in source_urls:
                    source_urls.append(value)
                elif isinstance(value, dict):
                    url = str(value.get("url") or value.get("source_url") or "")
                    if url and url not in source_urls:
                        source_urls.append(url)

        creative_sources = self.connection.execute(*sources_query).fetchall()
        for row in creative_sources:
            if row["source_url"] not in source_urls:
                source_urls.append(str(row["source_url"]))

        verified_aliases = sorted({row["candidate_id"] for row in link_rows})
        verified_placeholders = ",".join("?" for _ in verified_aliases) or "NULL"
        cutoff = datetime.fromisoformat(now) - timedelta(days=90)
        historical_rows = self.connection.execute(
            f"""
            SELECT MAX(observed_at) AS observed_at, surface, coverage_type
            FROM candidate_ad_observations o
            JOIN ad_discovery_runs r ON r.id=o.run_id
            WHERE o.candidate_id IN ({verified_placeholders}) AND o.identity_state='verified'
              AND o.identity_confidence>=80
              AND o.observed_at>=? AND o.observed_at<=?
              AND r.status IN ('complete', 'partial')
            GROUP BY substr(observed_at,1,7),surface,coverage_type
            ORDER BY observed_at
            """,
            (*verified_aliases, cutoff.isoformat(), now),
        ).fetchall()
        windows: set[str] = set()
        scoreable_windows: set[str] = set()
        for row in historical_rows:
            try:
                observed = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            if observed >= cutoff:
                windows.add(observed.strftime("%Y-%m"))
                if (
                    str(row["surface"]) != "tiktok_top"
                    and str(row["coverage_type"]) != "curated_top_ads_positive_only"
                ):
                    scoreable_windows.add(observed.strftime("%Y-%m"))

        first_values = [row["first_started_at"] for row in verified_rows if row["first_started_at"]]
        latest_values = [
            row["latest_started_at"] for row in verified_rows if row["latest_started_at"]
        ]
        scoreable_rows = [
            row
            for row in verified_rows
            if str(row["surface"]) != "tiktok_top"
            and str(row["coverage_type"]) != "curated_top_ads_positive_only"
        ]
        scoreable_first_values = [
            row["first_started_at"] for row in scoreable_rows if row["first_started_at"]
        ]
        scoreable_latest_values = [
            row["latest_started_at"] for row in scoreable_rows if row["latest_started_at"]
        ]
        first_started_at = min(first_values, default=None)
        latest_started_at = max(latest_values, default=None)
        scoreable_first_started_at = min(scoreable_first_values, default=None)
        scoreable_latest_started_at = max(scoreable_latest_values, default=None)
        unique_creatives = len(creative_group_ids) + unkeyed_creatives
        active_unique_creatives = len(active_creative_group_ids) + unkeyed_active_creatives
        scoreable_unique_creatives = len(scoreable_group_ids) + scoreable_unkeyed_creatives
        scoreable_active_unique_creatives = (
            len(scoreable_active_group_ids) + scoreable_unkeyed_active_creatives
        )
        scoreable_spans_ninety_days = False
        if scoreable_first_started_at and scoreable_latest_started_at:
            try:
                first_date = datetime.fromisoformat(scoreable_first_started_at)
                latest_date = datetime.fromisoformat(scoreable_latest_started_at)
                scoreable_spans_ninety_days = (latest_date - first_date).days >= 90
            except (TypeError, ValueError):
                pass
        return {
            "identity_state": identity_state,
            "identity_confidence": identity_confidence,
            "advertiser_ids": advertiser_ids,
            "advertiser_names": advertiser_names,
            "meta_page_id": str(meta_row["external_id"]) if meta_row else "",
            "meta_page_name": str(meta_row["display_name"]) if meta_row else "",
            "meta_page_url": str(meta_row["profile_url"]) if meta_row else "",
            "linked_instagram_handle": (
                str(meta_row["linked_instagram_handle"]) if meta_row else ""
            ),
            "platforms": platform_values,
            "total_ads": sum(int(row["total_ad_count"] or 0) for row in verified_rows),
            "active_ads": sum(int(row["active_ad_count"] or 0) for row in verified_rows),
            "unique_creatives": unique_creatives,
            "active_unique_creatives": active_unique_creatives,
            "video_creatives": sum(int(row["video_creatives"] or 0) for row in verified_rows),
            "image_creatives": sum(int(row["image_creatives"] or 0) for row in verified_rows),
            "new_creatives_7d": sum(int(row["new_creatives_7d"] or 0) for row in verified_rows),
            "new_creatives_30d": sum(int(row["new_creatives_30d"] or 0) for row in verified_rows),
            "new_creatives_90d": sum(int(row["new_creatives_90d"] or 0) for row in verified_rows),
            "observation_windows_90d": len(windows),
            "scoreable_platforms": scoreable_platform_values,
            "scoreable_total_ads": sum(int(row["total_ad_count"] or 0) for row in scoreable_rows),
            "scoreable_active_ads": sum(int(row["active_ad_count"] or 0) for row in scoreable_rows),
            "scoreable_unique_creatives": scoreable_unique_creatives,
            "scoreable_active_unique_creatives": scoreable_active_unique_creatives,
            "scoreable_new_creatives_7d": sum(
                int(row["new_creatives_7d"] or 0) for row in scoreable_rows
            ),
            "scoreable_new_creatives_30d": sum(
                int(row["new_creatives_30d"] or 0) for row in scoreable_rows
            ),
            "scoreable_new_creatives_90d": sum(
                int(row["new_creatives_90d"] or 0) for row in scoreable_rows
            ),
            "scoreable_observation_windows_90d": len(scoreable_windows),
            "first_started_at": first_started_at,
            "latest_started_at": latest_started_at,
            "scoreable_first_started_at": scoreable_first_started_at,
            "scoreable_latest_started_at": scoreable_latest_started_at,
            "last_observed_at": max(
                (str(row["observed_at"]) for row in verified_rows), default=None
            ),
            "recurring_established": len(scoreable_windows) >= 3
            or (scoreable_unique_creatives >= 6 and scoreable_spans_ninety_days),
            "source_urls": source_urls[:30],
            "coverage_notes": coverage_notes[:20],
        }

    def ad_observation_candidate_ids(self, run_id: int) -> list[int]:
        """Return candidates touched by one paid-ad run for terminal reprojection."""
        rows = self.connection.execute(
            """
            SELECT DISTINCT candidate_id
            FROM candidate_ad_observations
            WHERE run_id=?
            ORDER BY candidate_id
            """,
            (run_id,),
        ).fetchall()
        return [int(row["candidate_id"]) for row in rows]

    def find_candidate_by_canonical_domain(self, domain: str) -> dict[str, Any] | None:
        """Resolve one existing candidate without creating or changing source lineage."""
        normalized = self._normalize_identity_host(domain)
        if not normalized:
            return None
        row = self.connection.execute(
            """
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            )
            SELECT c.id AS candidate_id, c.hostname, c.source_apex, c.label_hint,
                   c.discovery_evidence_json, c.provider_record_ids_json,
                   s.observed_at, s.observation_json,
                   ti.canonical_domain, ti.target_score, ti.relationship_lane,
                   ti.target_scope
            FROM candidates c
            LEFT JOIN latest l ON l.candidate_id=c.id
            LEFT JOIN snapshots s ON s.id=l.snapshot_id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE lower(COALESCE(ti.canonical_domain, ''))=?
               OR lower(c.hostname) IN (?, ?)
            ORDER BY CASE WHEN lower(COALESCE(ti.canonical_domain, ''))=? THEN 0 ELSE 1 END,
                     CASE WHEN s.id IS NOT NULL THEN 0 ELSE 1 END,
                     COALESCE(ti.target_score, 0) DESC,
                     s.observed_at DESC, c.id DESC
            LIMIT 1
            """,
            (normalized, normalized, f"www.{normalized}", normalized),
        ).fetchone()
        if row is None:
            return None
        observation = (
            SiteObservation.model_validate_json(row["observation_json"])
            if row["observation_json"]
            else None
        )
        canonical_domain = row["canonical_domain"] or normalized
        return {
            "candidate_id": int(row["candidate_id"]),
            "hostname": row["hostname"],
            "source_apex": row["source_apex"],
            "label_hint": row["label_hint"],
            "canonical_domain": canonical_domain,
            "company_name": concise_company_name(
                label_hint=row["label_hint"] or "",
                title=observation.title if observation else "",
                canonical_domain=canonical_domain,
                hostname=row["hostname"],
            ),
            "social_urls": observation.social_urls if observation else [],
            "observed_at": row["observed_at"],
            "target_score": int(row["target_score"] or 0),
            "relationship_lane": row["relationship_lane"],
            "target_scope": row["target_scope"],
            "discovery_evidence_urls": json.loads(row["discovery_evidence_json"] or "[]"),
            "provider_record_ids": json.loads(row["provider_record_ids_json"] or "[]"),
        }

    @staticmethod
    def _ad_hydration_filter(
        *,
        lane: str,
        scope: str,
        min_score: int,
        only_without_verified_link: bool,
    ) -> tuple[str, list[Any]]:
        if lane not in {*TARGET_RELATIONSHIP_LANES, "all"}:
            raise ValueError("lane must be buyer, referral_partner, or all")
        if scope not in {*TARGET_SCOPES, "all", ""}:
            raise ValueError(
                "scope must be direct, adjacency, unclassified, out_of_scope, or all"
            )
        conditions = [
            "ti.live=1",
            "ti.disqualified=0",
            "COALESCE(w.excluded, 0)=0",
            "target_is_suppressed(c.source_apex, c.hostname, ti.canonical_domain)=0",
            "ti.target_score>=?",
        ]
        params: list[Any] = [max(0, min(int(min_score), 100))]
        if lane != "all":
            conditions.append("ti.relationship_lane=?")
            params.append(lane)
        if scope not in {"", "all"}:
            conditions.append("ti.target_scope=?")
            params.append(scope)
        if only_without_verified_link:
            conditions.append(
                "NOT EXISTS(SELECT 1 FROM advertiser_domain_links adl "
                "WHERE adl.candidate_id=c.id AND adl.state='verified' "
                "AND adl.confidence>=80)"
            )
        return " AND ".join(conditions), params

    @staticmethod
    def _ad_hydration_ctes(where: str, *, select_seeds: bool = False) -> str:
        seed_fields = """
                       s.id AS snapshot_id, s.observed_at,
                       ti.canonical_domain, ti.target_score,
                       ti.relationship_lane, ti.target_scope, ti.review_ready,
                       ti.principal_signals_met, ti.data_confidence,
                       COALESCE(ti.durable_fit_score, ti.target_score) AS durable_fit_score,
                       COALESCE(ti.contact_now_score, ti.target_score) AS contact_now_score,
                       COALESCE(ti.founder_priority_score, ti.target_score)
                           AS founder_priority_score,
                       COALESCE(ti.opportunity_risk, 0) AS opportunity_risk,
                       COALESCE(ti.opportunity_uncertainty, 100)
                           AS opportunity_uncertainty,
                       COALESCE(ti.recommended_action, 'research_next')
                           AS recommended_action,
                       COALESCE(json_extract(ti.hypothesis_json, '$.next_best_scan'), 'none')
                           AS next_best_scan,
                       CASE WHEN NOT EXISTS(
                           SELECT 1 FROM advertiser_domain_links adl
                           WHERE adl.candidate_id=c.id AND adl.state='verified'
                             AND adl.confidence>=80
                       ) THEN 1 ELSE 0 END AS missing_verified_advertiser,
        """ if select_seeds else ""
        return f"""
            WITH meta_attempts AS (
                SELECT CAST(
                           substr(q.query_key, length('meta-existing:') + 1)
                           AS INTEGER
                       ) AS candidate_id,
                       q.status, q.updated_at, r.status AS run_status,
                       ROW_NUMBER() OVER (
                           PARTITION BY CAST(
                               substr(q.query_key, length('meta-existing:') + 1)
                               AS INTEGER
                           )
                           ORDER BY q.updated_at DESC, q.id DESC
                       ) AS attempt_rank
                FROM ad_discovery_queries q
                JOIN ad_discovery_runs r ON r.id=q.run_id
                WHERE q.platform='meta'
                  AND q.phase='existing_target_lookup'
                  AND q.query_key GLOB 'meta-existing:[0-9]*'
            ), eligible AS (
                SELECT c.id AS candidate_id, {seed_fields}
                       ROW_NUMBER() OVER (
                           PARTITION BY ti.canonical_domain
                           ORDER BY COALESCE(ti.founder_priority_score, ti.target_score) DESC,
                                    COALESCE(ti.durable_fit_score, ti.target_score) DESC,
                                    ti.review_ready DESC, ti.target_score DESC,
                                    ti.principal_signals_met DESC,
                                    ti.data_confidence DESC, s.observed_at DESC,
                                    c.discovery_rank DESC, c.id DESC
                       ) AS identity_rank
                FROM candidates c
                CROSS JOIN snapshots s ON s.id=(
                    SELECT MAX(newer.id) FROM snapshots newer WHERE newer.candidate_id=c.id
                )
                CROSS JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                WHERE {where}
            )
        """

    def ad_hydration_inventory(
        self,
        *,
        lane: str = "buyer",
        scope: str = "direct",
        min_score: int = 0,
        only_without_verified_link: bool = False,
    ) -> dict[str, int]:
        """Count the complete existing-target Meta backlog without provider calls."""

        where, params = self._ad_hydration_filter(
            lane=lane,
            scope=scope,
            min_score=min_score,
            only_without_verified_link=only_without_verified_link,
        )
        now = datetime.now(UTC)
        success_cutoff = (now - REHYDRATION_INTERVAL).isoformat()
        failure_cutoff = (now - timedelta(hours=24)).isoformat()
        row = self.connection.execute(
            self._ad_hydration_ctes(where)
            + """
            SELECT COUNT(*) AS total_candidates,
                   SUM(CASE
                       WHEN m.run_status IN ('queued', 'running', 'cancelling') THEN 1
                       ELSE 0
                   END) AS active_candidates,
                   SUM(CASE
                       WHEN m.run_status NOT IN ('queued', 'running', 'cancelling')
                        AND m.status='complete' AND m.updated_at>? THEN 1
                       ELSE 0
                   END) AS fresh_candidates,
                   SUM(CASE
                       WHEN m.run_status NOT IN ('queued', 'running', 'cancelling')
                        AND m.status<>'complete' AND m.updated_at>? THEN 1
                       ELSE 0
                   END) AS retry_deferred_candidates,
                   SUM(CASE
                       WHEN m.candidate_id IS NULL THEN 1
                       WHEN m.run_status IN ('queued', 'running', 'cancelling') THEN 0
                       WHEN m.status='complete' AND m.updated_at>? THEN 0
                       WHEN m.status<>'complete' AND m.updated_at>? THEN 0
                       ELSE 1
                   END) AS due_candidates
            FROM eligible e
            LEFT JOIN meta_attempts m
              ON m.candidate_id=e.candidate_id AND m.attempt_rank=1
            WHERE e.identity_rank=1
            """,
            [
                *params,
                success_cutoff,
                failure_cutoff,
                success_cutoff,
                failure_cutoff,
            ],
        ).fetchone()
        return {
            "total_candidates": int(row["total_candidates"] or 0),
            "due_candidates": int(row["due_candidates"] or 0),
            "fresh_candidates": int(row["fresh_candidates"] or 0),
            "retry_deferred_candidates": int(row["retry_deferred_candidates"] or 0),
            "active_candidates": int(row["active_candidates"] or 0),
            "maximum_per_run": 500,
        }

    def select_ad_hydration_seeds(
        self,
        *,
        lane: str = "buyer",
        scope: str = "direct",
        min_score: int = 0,
        only_without_verified_link: bool = False,
        only_due: bool = False,
        limit: int = 100,
        candidate_ids: list[int] | None = None,
        include_details: bool = True,
    ) -> list[dict[str, Any]]:
        """Select current canonical targets for explicitly budgeted advertiser lookup."""
        where, params = self._ad_hydration_filter(
            lane=lane,
            scope=scope,
            min_score=min_score,
            only_without_verified_link=only_without_verified_link,
        )
        if candidate_ids is not None:
            if not candidate_ids:
                return []
            ids = list(dict.fromkeys(int(value) for value in candidate_ids))
            where += " AND c.id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        now = datetime.now(UTC)
        success_cutoff = (now - REHYDRATION_INTERVAL).isoformat()
        failure_cutoff = (now - timedelta(hours=24)).isoformat()
        due_filter = ""
        schedule_params: list[Any] = []
        if only_due:
            due_filter = """
              AND (
                    m.candidate_id IS NULL
                    OR (
                        m.run_status NOT IN ('queued', 'running', 'cancelling')
                        AND (
                            (m.status='complete' AND m.updated_at<=?)
                            OR (m.status<>'complete' AND m.updated_at<=?)
                        )
                    )
              )
            """
            schedule_params = [success_cutoff, failure_cutoff]
        order = """
            information_gain_score DESC,
            e.missing_verified_advertiser DESC,
            e.durable_fit_score DESC, e.founder_priority_score DESC,
            e.review_ready DESC, e.target_score DESC,
            e.principal_signals_met DESC, e.data_confidence DESC,
            e.observed_at DESC, e.candidate_id DESC
        """
        fields = "e.*" if include_details else "e.candidate_id"
        page = f"""
            SELECT {fields},
                   ROUND(
                       e.durable_fit_score * 0.45
                       + e.founder_priority_score * 0.15
                       + e.opportunity_uncertainty * 0.15
                       + e.data_confidence * 0.05
                       + e.missing_verified_advertiser * 20
                       + CASE WHEN e.next_best_scan='verified_paid_ad_activity'
                           THEN 15 ELSE 0 END
                       - e.opportunity_risk * 0.25
                   ) AS information_gain_score
            FROM eligible e
            LEFT JOIN meta_attempts m
              ON m.candidate_id=e.candidate_id AND m.attempt_rank=1
            WHERE e.identity_rank=1
            {due_filter}
            ORDER BY {order}
            LIMIT ?
        """
        # Select the same ordered page before loading contact and discovery payloads.
        sql = self._ad_hydration_ctes(where, select_seeds=True)
        if include_details:
            sql += f"""
                , page AS MATERIALIZED ({page})
                SELECT e.*,c.hostname,c.source_apex,c.label_hint,
                       c.discovery_evidence_json,c.provider_record_ids_json,s.observation_json,
                       (SELECT COUNT(*) FROM advertiser_domain_links adl
                        WHERE adl.candidate_id=e.candidate_id
                          AND adl.state='verified' AND adl.confidence>=80)
                           AS verified_advertiser_count
                FROM page e JOIN candidates c ON c.id=e.candidate_id
                JOIN snapshots s ON s.id=e.snapshot_id
                ORDER BY {order}
            """
        else:
            sql += page
        rows = self.connection.execute(
            sql,
            [*params, *schedule_params, max(1, min(int(limit), 1_000))],
        ).fetchall()
        if not include_details:
            return [{"candidate_id": int(row["candidate_id"])} for row in rows]
        seeds: list[dict[str, Any]] = []
        for row in rows:
            observation = SiteObservation.model_validate_json(row["observation_json"])
            seeds.append(
                {
                    "candidate_id": int(row["candidate_id"]),
                    "hostname": row["hostname"],
                    "source_apex": row["source_apex"],
                    "canonical_domain": row["canonical_domain"],
                    "company_name": concise_company_name(
                        label_hint=row["label_hint"] or "",
                        title=observation.title,
                        canonical_domain=row["canonical_domain"] or "",
                        hostname=row["hostname"],
                    ),
                    "social_urls": observation.social_urls,
                    "observed_at": row["observed_at"],
                    "target_score": int(row["target_score"]),
                    "durable_fit_score": int(row["durable_fit_score"] or 0),
                    "contact_now_score": int(row["contact_now_score"] or 0),
                    "founder_priority_score": int(row["founder_priority_score"] or 0),
                    "opportunity_risk": int(row["opportunity_risk"] or 0),
                    "opportunity_uncertainty": int(row["opportunity_uncertainty"] or 0),
                    "recommended_action": row["recommended_action"],
                    "next_best_scan": row["next_best_scan"],
                    "information_gain_score": int(row["information_gain_score"] or 0),
                    "relationship_lane": row["relationship_lane"],
                    "target_scope": row["target_scope"],
                    "verified_advertiser_count": int(row["verified_advertiser_count"] or 0),
                    "discovery_evidence_urls": json.loads(row["discovery_evidence_json"] or "[]"),
                    "provider_record_ids": json.loads(row["provider_record_ids_json"] or "[]"),
                }
            )
        return seeds

    def intelligence_payload(self) -> dict[str, Any]:
        latest = self.connection.execute(
            "SELECT * FROM source_scout_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        counts = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted
            FROM source_suggestions
            WHERE duplicate_of_suggestion_id IS NULL
            """
        ).fetchone()
        cache_count = int(self.connection.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0])
        free_cache_count = int(
            self.connection.execute("SELECT COUNT(*) FROM free_signal_cache").fetchone()[0]
        )
        count_values = dict(counts)
        return {
            "suggestions": self.list_source_suggestions(),
            "counts": {key: int(value or 0) for key, value in count_values.items()},
            "ai_cache_entries": cache_count,
            "free_cache_entries": free_cache_count,
            "latest_scout": (
                {
                    **dict(latest),
                    "summary": json.loads(latest["summary_json"]) if latest["summary_json"] else {},
                }
                if latest
                else None
            ),
        }

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            WITH source_catalog AS (
                SELECT apex, category, tier, indexed_names, expected_fit, notes,
                       enabled, registry_managed, created_at, updated_at
                FROM managed_sources
                UNION ALL
                SELECT c.source_apex AS apex,
                       CASE
                           WHEN c.source_apex IN ('openstreetmap.org', 'wikidata.org')
                           THEN 'Agency'
                           WHEN c.source_apex IN (
                               'meta-ad-library',
                               'tiktok-commercial-content-library',
                               'tiktok-top-ads'
                           ) THEN 'Ecommerce'
                           ELSE 'Derived'
                       END AS category,
                       'Public' AS tier,
                       NULL AS indexed_names,
                       'Placement discovery' AS expected_fit,
                       CASE c.source_apex
                           WHEN 'openstreetmap.org' THEN 'OpenStreetMap public records'
                           WHEN 'wikidata.org' THEN 'Wikidata public records'
                           WHEN 'agency-portfolio' THEN 'First-party portfolio references'
                           WHEN 'meta-ad-library' THEN 'Meta paid-ad discovery'
                           WHEN 'tiktok-commercial-content-library'
                           THEN 'TikTok commercial-content discovery'
                           WHEN 'tiktok-top-ads' THEN 'TikTok curated Top Ads discovery'
                           ELSE 'Cited public identity recoveries'
                       END AS notes,
                       1 AS enabled,
                       0 AS registry_managed,
                       MIN(c.discovered_at) AS created_at,
                       MAX(c.last_discovered_at) AS updated_at
                FROM candidates c
                WHERE c.source_apex IN (
                    'openstreetmap.org', 'wikidata.org', 'agency-portfolio', 'ai-recovery',
                    'meta-ad-library', 'tiktok-commercial-content-library',
                    'tiktok-top-ads'
                )
                  AND NOT EXISTS(
                      SELECT 1 FROM managed_sources existing
                      WHERE existing.apex=c.source_apex
                  )
                GROUP BY c.source_apex
            ), visible_sources AS MATERIALIZED (
                SELECT ms.* FROM source_catalog ms
                WHERE NOT EXISTS (
                    SELECT 1 FROM source_suggestions suggestion
                    WHERE suggestion.duplicate_of_suggestion_id IS NULL
                      AND suggestion.status='accepted'
                      AND suggestion.source_kind='tenant_apex'
                      AND lower(rtrim(suggestion.apex, '.'))=lower(rtrim(ms.apex, '.'))
                )
            ), source_scan_reliability AS (
                SELECT source.apex,
                       COUNT(execution.id) AS scan_attempt_count,
                       SUM(CASE WHEN execution.status IN (
                           'transient_error', 'rate_limited', 'robots_blocked',
                           'identity_ambiguous', 'failed'
                       ) THEN 1 ELSE 0 END) AS scan_failure_count
                FROM visible_sources source
                CROSS JOIN candidate_source_edges edge ON edge.source_apex=source.apex
                CROSS JOIN scanner_executions execution
                  ON execution.candidate_id=edge.candidate_id
                WHERE execution.status IN (
                    'success', 'success_empty', 'partial', 'transient_error',
                    'rate_limited', 'robots_blocked', 'identity_ambiguous', 'failed'
                )
                GROUP BY source.apex
            )
            SELECT ms.*,
                   COUNT(c.id) AS candidate_count,
                   COUNT(s.id) AS inspected_count,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.observation_json, '$.live') THEN 1 ELSE 0 END
                   ), 0) AS live_count,
                   COALESCE(SUM(
                       CASE WHEN s.final_score >= 70 THEN 1 ELSE 0 END
                   ), 0) AS qualified_count,
                   COALESCE(SUM(
                       CASE WHEN COALESCE(
                           json_extract(s.score_json, '$.contact_priority'), s.final_score
                       ) >= 70 THEN 1 ELSE 0 END
                   ), 0) AS high_priority_count,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.score_json, '$.budget_ready') THEN 1 ELSE 0 END
                   ), 0) AS budget_ready_count,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.score_json, '$.placement.ready') THEN 1 ELSE 0 END
                   ), 0) AS placement_ready_count,
                   COALESCE(SUM(
                       CASE WHEN COALESCE(
                           json_extract(
                               s.observation_json, '$.contacts.preferred_email'
                           ), ''
                       ) <> '' OR COALESCE(
                           json_array_length(json_extract(s.observation_json, '$.contact_urls')), 0
                       ) > 0 THEN 1 ELSE 0 END
                   ), 0) AS contactable_count,
                   COALESCE(AVG(COALESCE(
                       json_extract(s.score_json, '$.contact_priority'), s.final_score
                   )), 0) AS average_priority,
                   (
                       SELECT COUNT(DISTINCT outbound.candidate_id)
                       FROM prospect_activities outbound
                       JOIN candidates outbound_candidate
                         ON outbound_candidate.id=outbound.candidate_id
                       WHERE outbound_candidate.source_apex=ms.apex
                         AND outbound.activity_type IN (
                             'email_sent', 'linkedin_message', 'call'
                         )
                   ) AS outbound_accounts,
                   (
                       SELECT COUNT(DISTINCT reply.candidate_id)
                       FROM prospect_activities reply
                       JOIN candidates reply_candidate
                         ON reply_candidate.id=reply.candidate_id
                       WHERE reply_candidate.source_apex=ms.apex
                         AND reply.activity_type='reply'
                   ) AS replied_accounts,
                   (
                       SELECT COUNT(DISTINCT meeting.candidate_id)
                       FROM prospect_activities meeting
                       JOIN candidates meeting_candidate
                         ON meeting_candidate.id=meeting.candidate_id
                       WHERE meeting_candidate.source_apex=ms.apex
                         AND meeting.activity_type='meeting'
                   ) AS meeting_accounts,
                   COALESCE(MAX(reliability.scan_attempt_count), 0)
                       AS scan_attempt_count,
                   COALESCE(MAX(reliability.scan_failure_count), 0)
                       AS scan_failure_count,
                   MAX(s.observed_at) AS last_observed_at
            FROM visible_sources ms
            LEFT JOIN source_scan_reliability reliability ON reliability.apex=ms.apex
            LEFT JOIN candidates c ON c.source_apex=ms.apex
            LEFT JOIN snapshots s ON s.id=(
                SELECT MAX(newer.id) FROM snapshots newer WHERE newer.candidate_id=c.id
            )
            GROUP BY ms.apex
            ORDER BY CASE ms.tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'B/C' THEN 2 ELSE 3 END,
                     ms.category, ms.apex
            """
        ).fetchall()
        sources = []
        for row in rows:
            source = dict(row)
            inspected = int(source["inspected_count"] or 0)
            if inspected:
                live_rate = int(source["live_count"] or 0) / inspected
                high_priority_rate = int(source["high_priority_count"] or 0) / inspected
                budget_rate = int(source["budget_ready_count"] or 0) / inspected
                placement_rate = int(source["placement_ready_count"] or 0) / inspected
                contact_rate = int(source["contactable_count"] or 0) / inspected
                average_priority = float(source["average_priority"] or 0) / 100
                raw_score = 100 * (
                    0.15 * live_rate
                    + 0.25 * high_priority_rate
                    + 0.30 * placement_rate
                    + 0.15 * contact_rate
                    + 0.15 * average_priority
                )
                confidence = min(inspected / 50, 1.0)
                opportunity_score = round(20 * (1 - confidence) + raw_score * confidence)
            else:
                live_rate = high_priority_rate = budget_rate = placement_rate = contact_rate = 0.0
                confidence = 0.0
                opportunity_score = 20
            outbound_accounts = int(source["outbound_accounts"] or 0)
            replied_accounts = int(source["replied_accounts"] or 0)
            meeting_accounts = int(source["meeting_accounts"] or 0)
            outcome_confidence = (
                min(outbound_accounts / 50, 1.0) if outbound_accounts >= 20 else 0.0
            )
            reply_yield = replied_accounts / outbound_accounts if outbound_accounts else 0.0
            meeting_yield = meeting_accounts / outbound_accounts if outbound_accounts else 0.0
            source.update(
                {
                    "enabled": bool(row["enabled"]),
                    "registry_managed": bool(row["registry_managed"]),
                    "live_rate": round(live_rate * 100, 1),
                    "high_priority_yield": round(high_priority_rate * 100, 1),
                    "budget_ready_yield": round(budget_rate * 100, 1),
                    "placement_ready_yield": round(placement_rate * 100, 1),
                    "contactable_yield": round(contact_rate * 100, 1),
                    "average_priority": round(float(source["average_priority"] or 0), 1),
                    "sample_confidence": round(confidence * 100, 1),
                    "outbound_accounts": outbound_accounts,
                    "replied_accounts": replied_accounts,
                    "meeting_accounts": meeting_accounts,
                    "reply_yield": round(reply_yield * 100, 1),
                    "meeting_yield": round(meeting_yield * 100, 1),
                    "outcome_sample_confidence": round(outcome_confidence * 100, 1),
                    "opportunity_score": max(0, min(opportunity_score, 100)),
                    "scan_attempt_count": int(source["scan_attempt_count"] or 0),
                    "scan_failure_count": int(source["scan_failure_count"] or 0),
                    "scan_failure_rate": round(
                        int(source["scan_failure_count"] or 0)
                        / int(source["scan_attempt_count"] or 1)
                        * 100,
                        1,
                    )
                    if int(source["scan_attempt_count"] or 0)
                    else 0.0,
                }
            )
            sources.append(source)
        return sources

    def enabled_sources(self) -> list[str]:
        return [
            str(row["apex"])
            for row in self.connection.execute(
                """
                SELECT managed.apex
                FROM managed_sources managed
                WHERE managed.enabled=1
                  AND NOT EXISTS (
                      SELECT 1
                      FROM source_suggestions suggestion
                      WHERE suggestion.duplicate_of_suggestion_id IS NULL
                        AND suggestion.status='accepted'
                        AND suggestion.source_kind='tenant_apex'
                        AND lower(rtrim(suggestion.apex, '.'))=
                            lower(rtrim(managed.apex, '.'))
                  )
                ORDER BY managed.tier, managed.apex
                """
            ).fetchall()
        ]

    def scan_capacity(self, sources: list[str], max_hosts_per_source: int) -> int:
        if not sources:
            return 0
        placeholders = ", ".join("?" for _ in sources)
        rows = self.connection.execute(
            f"SELECT apex, indexed_names FROM managed_sources WHERE apex IN ({placeholders})",
            sources,
        ).fetchall()
        indexed_by_apex = {str(row["apex"]): row["indexed_names"] for row in rows}
        return sum(
            min(int(indexed_by_apex[source]), max_hosts_per_source)
            if indexed_by_apex.get(source) is not None
            else max_hosts_per_source
            for source in sources
        )

    def active_run_progress(self, job_id: str | None = None) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT r.*, p.phase, p.discovered, p.inspected, p.enriched,
                   p.external_checked, p.total_candidates,
                   p.updated_at AS progress_updated_at
            FROM runs r
            LEFT JOIN run_progress p ON p.run_id=r.id
            WHERE r.status='running' ORDER BY r.id DESC
            """
        ).fetchall()
        row = next(
            (
                candidate
                for candidate in rows
                if job_id is None or json.loads(candidate["config_json"]).get("job_id") == job_id
            ),
            None,
        )
        if row is None:
            return {}
        config = json.loads(row["config_json"])
        sources = list(config.get("apexes", []))
        maximum = self.scan_capacity(
            sources,
            int(config.get("max_hosts_per_source", 0)),
        )
        saved = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE run_id=?", (row["id"],)
            ).fetchone()[0]
        )
        phase = str(row["phase"] or "discovering")
        if phase == "discovering":
            processed = int(row["discovered"] or 0)
        elif phase == "free_intelligence":
            processed = int(row["enriched"] or 0)
        elif phase in {
            "agency_discovery",
            "archive_footprint",
            "news_intelligence",
            "ai_review",
            "frontier_intelligence",
        }:
            processed = int(row["external_checked"] or 0)
        else:
            processed = max(int(row["inspected"] or 0), saved)
        total_candidates = int(row["total_candidates"] or 0)
        if phase == "archive_footprint":
            denominator = total_candidates or int(config.get("max_common_crawl_lookups", 0))
        elif phase == "news_intelligence":
            denominator = total_candidates or int(config.get("max_news_lookups", 0))
        else:
            denominator = total_candidates or maximum
        percent = min(100.0, round(processed / denominator * 100, 1)) if denominator else 0.0
        return {
            "run_id": int(row["id"]),
            "config": config,
            "started_at": row["started_at"],
            "processed": processed,
            "discovered": int(row["discovered"] or 0),
            "total_candidates": total_candidates,
            "progress_updated_at": row["progress_updated_at"],
            "phase": phase,
            "inspected": int(row["inspected"] or 0),
            "enriched": int(row["enriched"] or 0),
            "external_checked": int(row["external_checked"] or 0),
            "max_candidates": maximum,
            "progress_percent": percent,
            "source_count": len(sources),
            "sources": sources,
        }

    def run_metadata_for_job(self, job_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT id, config_json FROM runs ORDER BY id DESC"
        ).fetchall()
        run_id = next(
            (
                int(row["id"])
                for row in rows
                if json.loads(row["config_json"]).get("job_id") == job_id
            ),
            None,
        )
        return self.run_metadata(run_id) if run_id is not None else {}

    def update_workflow(
        self,
        candidate_id: int,
        *,
        stage: str | None = None,
        starred: bool | None = None,
        excluded: bool | None = None,
        scan_enabled: bool | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        next_follow_up_at: str | None = None,
        update_next_follow_up: bool = False,
    ) -> dict[str, Any] | None:
        candidate = self.connection.execute(
            "SELECT id FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not candidate:
            return None
        current = self.connection.execute(
            "SELECT * FROM prospect_workflow WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        current_stage = str(current["stage"]) if current else "research"
        current_starred = bool(current["starred"]) if current else False
        current_excluded = bool(current["excluded"]) if current else False
        current_scan_enabled = bool(current["scan_enabled"]) if current else True
        current_notes = str(current["notes"]) if current else ""
        current_follow_up = current["next_follow_up_at"] if current else None
        timestamp = _now()
        self.connection.execute(
            """
            INSERT INTO prospect_workflow(
                candidate_id, stage, starred, excluded, scan_enabled, notes,
                next_follow_up_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                stage=excluded.stage,
                starred=excluded.starred,
                excluded=excluded.excluded,
                scan_enabled=excluded.scan_enabled,
                notes=excluded.notes,
                next_follow_up_at=excluded.next_follow_up_at,
                updated_at=excluded.updated_at
            """,
            (
                candidate_id,
                stage if stage is not None else current_stage,
                int(starred if starred is not None else current_starred),
                int(excluded if excluded is not None else current_excluded),
                int(scan_enabled if scan_enabled is not None else current_scan_enabled),
                notes if notes is not None else current_notes,
                next_follow_up_at if update_next_follow_up else current_follow_up,
                timestamp,
            ),
        )
        normalized_tags = self._tags_for(candidate_id)
        if tags is not None:
            normalized_tags = _normalize_tags(tags)
            self.connection.execute(
                "DELETE FROM prospect_tags WHERE candidate_id=?", (candidate_id,)
            )
            self.connection.executemany(
                "INSERT INTO prospect_tags(candidate_id, tag, created_at) VALUES (?, ?, ?)",
                [(candidate_id, tag, timestamp) for tag in normalized_tags],
            )
        self.connection.commit()
        return {
            "candidate_id": candidate_id,
            "stage": stage if stage is not None else current_stage,
            "starred": starred if starred is not None else current_starred,
            "excluded": excluded if excluded is not None else current_excluded,
            "scan_enabled": (
                scan_enabled if scan_enabled is not None else current_scan_enabled
            ),
            "notes": notes if notes is not None else current_notes,
            "tags": normalized_tags,
            "next_follow_up_at": (
                next_follow_up_at if update_next_follow_up else current_follow_up
            ),
            "updated_at": timestamp,
        }

    def _tags_for(self, candidate_id: int) -> list[str]:
        return [
            str(row["tag"])
            for row in self.connection.execute(
                "SELECT tag FROM prospect_tags WHERE candidate_id=? ORDER BY tag",
                (candidate_id,),
            ).fetchall()
        ]

    def candidate_scan_reliability(self, candidate_id: int) -> dict[str, Any]:
        """Return durable terminal execution reliability for one candidate."""

        return self.candidate_scan_reliability_bulk([candidate_id]).get(
            int(candidate_id),
            {
                "scan_attempt_count": 0,
                "scan_failure_count": 0,
                "scan_failure_rate": 0.0,
                "scan_failure_by_type": [],
            },
        )

    def candidate_scan_reliability_bulk(
        self, candidate_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]:
        """Return terminal scan reliability without one query per candidate."""

        attempt_statuses = sorted(SCAN_RELIABILITY_ATTEMPT_STATUSES)
        failure_statuses = sorted(SCAN_RELIABILITY_FAILURE_STATUSES)
        attempt_placeholders = ", ".join("?" for _ in attempt_statuses)
        failure_placeholders = ", ".join("?" for _ in failure_statuses)
        unique_ids = list(dict.fromkeys(int(candidate_id) for candidate_id in candidate_ids))
        grouped: dict[int, list[sqlite3.Row]] = {candidate_id: [] for candidate_id in unique_ids}
        for start in range(0, len(unique_ids), 500):
            batch = unique_ids[start : start + 500]
            if not batch:
                continue
            candidate_placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT candidate_id, scanner_id,
                       COUNT(*) AS attempt_count,
                       SUM(CASE WHEN status IN ({failure_placeholders}) THEN 1 ELSE 0 END)
                           AS failure_count
                FROM scanner_executions
                WHERE candidate_id IN ({candidate_placeholders})
                  AND status IN ({attempt_placeholders})
                GROUP BY candidate_id, scanner_id
                ORDER BY candidate_id, scanner_id
                """,
                [*failure_statuses, *batch, *attempt_statuses],
            ).fetchall()
            for row in rows:
                grouped[int(row["candidate_id"])].append(row)

        result: dict[int, dict[str, Any]] = {}
        for candidate_id, rows in grouped.items():
            by_type = []
            attempts = 0
            failures = 0
            for row in rows:
                type_attempts = int(row["attempt_count"] or 0)
                type_failures = int(row["failure_count"] or 0)
                attempts += type_attempts
                failures += type_failures
                by_type.append(
                    {
                        "scanner_id": str(row["scanner_id"]),
                        "attempt_count": type_attempts,
                        "failure_count": type_failures,
                        "failure_rate": round(type_failures / type_attempts * 100, 1)
                        if type_attempts
                        else 0.0,
                    }
                )
            result[candidate_id] = {
                "scan_attempt_count": attempts,
                "scan_failure_count": failures,
                "scan_failure_rate": (
                    round(failures / attempts * 100, 1) if attempts else 0.0
                ),
                "scan_failure_by_type": by_type,
            }
        return result

    def list_tags(self) -> list[dict[str, Any]]:
        return [
            {"tag": str(row["tag"]), "count": int(row["count"])}
            for row in self.connection.execute(
                """
                SELECT tag, COUNT(*) AS count
                FROM prospect_tags GROUP BY tag ORDER BY count DESC, tag
                """
            ).fetchall()
        ]

    def add_activity(
        self,
        candidate_id: int,
        *,
        activity_type: str,
        summary: str,
        contact_email: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any] | None:
        if activity_type not in ACTIVITY_TYPES:
            raise ValueError("Unknown activity type")
        candidate = self.connection.execute(
            "SELECT id FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not candidate:
            return None
        timestamp = _now()
        occurred = occurred_at or timestamp
        cursor = self.connection.execute(
            """
            INSERT INTO prospect_activities(
                candidate_id, activity_type, summary, contact_email, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, activity_type, summary, contact_email, occurred, timestamp),
        )
        activity_id = int(cursor.lastrowid)
        if activity_type in OUTBOUND_ACTIVITY_TYPES:
            self._record_intelligence_exposure_uncommitted(
                candidate_id=candidate_id,
                activity_id=activity_id,
                channel=activity_type,
                occurred_at=occurred,
                created_at=timestamp,
            )
        current = self.connection.execute(
            "SELECT stage FROM prospect_workflow WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        stage = str(current["stage"]) if current else "research"
        if stage not in {"won", "lost"}:
            if activity_type in OUTBOUND_ACTIVITY_TYPES and stage in {"research", "qualified"}:
                stage = "contacted"
            elif activity_type == "reply" and stage in {
                "research",
                "qualified",
                "contacted",
            }:
                stage = "replied"
            elif activity_type == "meeting":
                stage = "meeting"
        self.connection.execute(
            """
            INSERT INTO prospect_workflow(candidate_id, stage, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                stage=excluded.stage, updated_at=excluded.updated_at
            """,
            (candidate_id, stage, timestamp),
        )
        self.connection.commit()
        return {
            "id": activity_id,
            "candidate_id": candidate_id,
            "activity_type": activity_type,
            "summary": summary,
            "contact_email": contact_email,
            "occurred_at": occurred,
            "created_at": timestamp,
            "stage": stage,
        }

    def _record_intelligence_exposure_uncommitted(
        self,
        *,
        candidate_id: int,
        activity_id: int,
        channel: str,
        occurred_at: str,
        created_at: str,
    ) -> None:
        """Freeze the exact machine packet seen when a real outbound touch is logged."""

        row = self.connection.execute(
            f"""
            SELECT s.id AS snapshot_id, s.observed_at, {TARGET_PROJECTION_SELECT}
            FROM snapshots s
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE s.candidate_id=?
            ORDER BY s.id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        target = self._target_intelligence_from_row(row) if row else None
        source_paths = [
            str(edge["source_apex"])
            for edge in self.connection.execute(
                """
                SELECT source_apex FROM candidate_source_edges
                WHERE candidate_id=? ORDER BY discovery_rank DESC, source_apex
                """,
                (candidate_id,),
            ).fetchall()
        ]
        if target:
            packet = target.get("decision_packet") or {}
            if channel == "call":
                message_angle = str(packet.get("call_opener") or "")
            else:
                message_angle = str(packet.get("email_angle") or target.get("why_now") or "")
            trigger_keys = [
                str(event.get("event_key"))
                for event in target.get("signal_events", [])
                if event.get("family") == "commercial_timing"
                and event.get("polarity") == "positive"
                and float(event.get("current_strength") or 0) > 0
            ][:10]
            rubric_version = str(target.get("rubric_version") or "")
            target_score = int(target.get("target_score") or 0)
            founder_priority = int(target.get("founder_priority_score") or 0)
            action = str(target.get("recommended_action") or "")
            projection_json = json.dumps(target, sort_keys=True)
        else:
            message_angle = ""
            trigger_keys = []
            rubric_version = ""
            target_score = 0
            founder_priority = 0
            action = ""
            projection_json = "{}"
        self.connection.execute(
            """
            INSERT INTO intelligence_exposures(
                candidate_id, activity_id, snapshot_id, channel, occurred_at,
                rubric_version, target_score, founder_priority_score,
                recommended_action, message_angle, trigger_event_keys_json,
                source_paths_json, projection_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                activity_id,
                int(row["snapshot_id"]) if row else None,
                channel,
                occurred_at,
                rubric_version,
                target_score,
                founder_priority,
                action,
                message_angle[:4_000],
                json.dumps(trigger_keys),
                json.dumps(source_paths[:20]),
                projection_json,
                created_at,
            ),
        )

    @staticmethod
    def _prospect_dict(row: sqlite3.Row) -> dict[str, Any]:
        observation = SiteObservation.model_validate_json(row["observation_json"])
        score = ScoreCard.model_validate_json(row["score_json"])
        target_intelligence = LeadStore._target_intelligence_from_row(row)
        if target_intelligence is not None:
            score.creative_capacity = CreativeCapacityHypothesis.model_validate(
                target_intelligence["hypothesis"]
            )
            score.archetype = target_intelligence["archetype"]
            score.company_role = type(score.company_role).model_validate(
                target_intelligence.get("company_role") or {}
            )
        ai = AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None
        company_name = concise_company_name(
            ai_name=ai.company_name if ai else "",
            label_hint=row["label_hint"] or "",
            title=observation.title,
            canonical_domain=observation.canonical_host,
            hostname=row["hostname"],
        )
        if ai and ai.company_url:
            company_url = ai.company_url
        elif observation.canonical_host and observation.canonical_host.removeprefix(
            "www."
        ) != observation.hostname.removeprefix("www."):
            company_url = f"https://{observation.canonical_host}"
        else:
            company_url = observation.final_url or f"https://{row['hostname']}"
        preferred = observation.contacts.preferred_email
        best_contact = next(
            (
                contact.model_dump(mode="json")
                for contact in observation.contacts.emails
                if contact.address == preferred
            ),
            None,
        )
        preferred_phone = observation.contacts.preferred_phone
        best_phone = next(
            (
                phone.model_dump(mode="json")
                for phone in observation.contacts.phones
                if phone.dial_number == preferred_phone
            ),
            None,
        )
        public_emails = [
            contact.address
            for contact in observation.contacts.emails
            if contact.outreach_eligible and not contact.historical
        ]
        return {
            "candidate_id": int(row["candidate_id"]),
            "hostname": row["hostname"],
            "source_apex": row["source_apex"],
            "label_hint": row["label_hint"],
            "discovery_evidence_urls": json.loads(row["discovery_evidence_json"] or "[]"),
            "provider_record_ids": json.loads(row["provider_record_ids_json"] or "[]"),
            "first_seen": row["first_seen"],
            "company_name": company_name,
            "company_url": company_url,
            "final_score": int(row["final_score"]),
            "archetype": score.archetype,
            "confidence": score.confidence,
            "disqualified": score.disqualified,
            "live": observation.live,
            "status_code": observation.status_code,
            "canonical_host": observation.canonical_host,
            "emails": public_emails,
            "contacts": observation.contacts.model_dump(mode="json"),
            "best_contact": best_contact,
            "best_phone": best_phone,
            "contact_urls": observation.contact_urls,
            "social_urls": observation.social_urls,
            "technologies": observation.technologies,
            "web": observation.web.model_dump(mode="json"),
            "score": score.model_dump(mode="json"),
            "target_intelligence": target_intelligence,
            "ai": ai.model_dump(mode="json") if ai else None,
            "stage": row["stage"],
            "starred": bool(row["starred"]),
            "excluded": bool(row["excluded"]),
            "scan_enabled": bool(row["scan_enabled"]),
            "notes": row["notes"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "contact_count": int(row["contact_count"] or 0),
            "last_contacted_at": row["last_contacted_at"],
            "next_follow_up_at": row["next_follow_up_at"],
            "observed_at": row["observed_at"],
            "run_id": int(row["run_id"]),
            "changed": bool(row["changed"]),
        }

    def list_prospects(
        self,
        *,
        search: str = "",
        source: str = "",
        stage: str = "",
        tag: str = "",
        min_score: int = 0,
        live: bool | None = None,
        starred: bool | None = None,
        excluded: bool | None = False,
        budget_ready: bool | None = None,
        placement_ready: bool | None = None,
        has_public_email: bool | None = None,
        archetype: str = "",
        min_placement: int = 0,
        min_demand: int = 0,
        min_capacity: int = 0,
        min_timing: int = 0,
        min_creative_capacity: int = 0,
        creative_review_ready: bool | None = None,
        sort: str = "placement",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = [f"{_research_priority_sql('s')} >= ?"]
        params: list[Any] = [min_score]
        if search:
            conditions.append(
                """(
                    lower(c.hostname) LIKE ? OR
                    lower(COALESCE(json_extract(s.observation_json, '$.title'), '')) LIKE ? OR
                    lower(COALESCE(json_extract(s.observation_json, '$.canonical_host'), '')) LIKE ?
                )"""
            )
            needle = f"%{search.lower()}%"
            params.extend([needle, needle, needle])
        if source:
            conditions.append("c.source_apex=?")
            params.append(source)
        if stage:
            conditions.append("COALESCE(w.stage, 'research')=?")
            params.append(stage)
        if tag:
            conditions.append(
                "EXISTS(SELECT 1 FROM prospect_tags pt WHERE pt.candidate_id=c.id AND pt.tag=?)"
            )
            params.append(tag.strip().lower())
        if live is not None:
            conditions.append("json_extract(s.observation_json, '$.live')=?")
            params.append(int(live))
        if starred is not None:
            conditions.append("COALESCE(w.starred, 0)=?")
            params.append(int(starred))
        if excluded is not None:
            conditions.append("COALESCE(w.excluded, 0)=?")
            params.append(int(excluded))
        if budget_ready is not None:
            conditions.append("COALESCE(json_extract(s.score_json, '$.budget_ready'), 0)=?")
            params.append(int(budget_ready))
        if placement_ready is not None:
            conditions.append("COALESCE(json_extract(s.score_json, '$.placement.ready'), 0)=?")
            params.append(int(placement_ready))
        if has_public_email is not None:
            operator = "IS NOT NULL" if has_public_email else "IS NULL"
            conditions.append(
                "json_extract(s.observation_json, '$.contacts.preferred_email') " + operator
            )
        if archetype:
            conditions.append("COALESCE(json_extract(s.score_json, '$.archetype'), 'unknown')=?")
            params.append(archetype)
        if min_placement:
            conditions.append("COALESCE(json_extract(s.score_json, '$.placement.score'), 0)>=?")
            params.append(min_placement)
        if min_demand:
            conditions.append("COALESCE(json_extract(s.score_json, '$.demand_score'), 0)>=?")
            params.append(min_demand)
        if min_capacity:
            conditions.append("COALESCE(json_extract(s.score_json, '$.capacity_score'), 0)>=?")
            params.append(min_capacity)
        if min_timing:
            conditions.append("COALESCE(json_extract(s.score_json, '$.timing_score'), 0)>=?")
            params.append(min_timing)
        if min_creative_capacity:
            conditions.append(
                "COALESCE(ti.target_score, "
                "json_extract(s.score_json, '$.creative_capacity.total'), 0)>=?"
            )
            params.append(min_creative_capacity)
        if creative_review_ready is not None:
            conditions.append(
                "COALESCE(ti.review_ready, json_extract("
                "s.score_json, '$.creative_capacity.review_ready'), 0)=?"
            )
            params.append(int(creative_review_ready))
        sort_expressions = {
            "placement": (
                "MAX(COALESCE(json_extract(s.score_json, '$.placement.score'), 0), "
                f"{_strategic_research_score_sql('s')})"
            ),
            "priority": _research_priority_sql("s"),
            "demand": "COALESCE(json_extract(s.score_json, '$.demand_score'), 0)",
            "capacity": "COALESCE(json_extract(s.score_json, '$.capacity_score'), 0)",
            "timing": "COALESCE(json_extract(s.score_json, '$.timing_score'), 0)",
            "creative_capacity": (
                "COALESCE(ti.target_score, "
                "json_extract(s.score_json, '$.creative_capacity.total'), 0)"
            ),
            "growth": "COALESCE(json_extract(s.score_json, '$.growth_score'), 0)",
            "confidence": "COALESCE(json_extract(s.score_json, '$.data_confidence'), 0)",
            "vendor_stack": (
                "COALESCE(json_extract(s.observation_json, '$.web.vendors.stack_score'), 0)"
            ),
            "creative_stack": (
                "COALESCE(json_extract(s.observation_json, "
                "'$.web.vendors.creative_commerce_score'), 0)"
            ),
            "osint": ("COALESCE(json_extract(s.observation_json, '$.web.sales.overall_score'), 0)"),
            "fit": "s.final_score",
            "changed": "changed",
        }
        order_expression = sort_expressions.get(sort, sort_expressions["priority"])
        where = " AND ".join(conditions)
        cte = f"""
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            ), deduped AS (
                SELECT candidate_id, snapshot_id
                FROM (
                    SELECT l.candidate_id, l.snapshot_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(
                                   NULLIF(
                                       lower(
                                           trim(
                                               json_extract(s.observation_json, '$.canonical_host')
                                           )
                                       ),
                                       ''
                                   ),
                                   lower(c.hostname)
                               )
                               ORDER BY {_research_priority_sql("s")} DESC,
                               s.final_score DESC, s.observed_at DESC, c.id DESC
                           ) AS identity_rank
                    FROM latest l
                    JOIN snapshots s ON s.id=l.snapshot_id
                    JOIN candidates c ON c.id=l.candidate_id
                ) ranked
                WHERE identity_rank=1
            )
        """
        joins = """
            FROM deduped l
            JOIN snapshots s ON s.id=l.snapshot_id
            JOIN candidates c ON c.id=l.candidate_id
            LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
        """
        total = self.connection.execute(
            f"{cte} SELECT COUNT(*) {joins} WHERE {where}", params
        ).fetchone()[0]
        rows = self.connection.execute(
            f"""
            {cte}
            SELECT c.id AS candidate_id, c.hostname, c.source_apex, c.first_seen,
                   c.label_hint, c.discovery_evidence_json, c.provider_record_ids_json,
                   s.run_id, s.observed_at, s.observation_json, s.score_json,
                   s.ai_json, s.final_score,
                   {TARGET_PROJECTION_SELECT},
                   COALESCE(w.stage, 'research') AS stage,
                   COALESCE(w.starred, 0) AS starred,
                   COALESCE(w.excluded, 0) AS excluded,
                   COALESCE(w.scan_enabled, 1) AS scan_enabled,
                   COALESCE(w.notes, '') AS notes,
                   w.next_follow_up_at,
                   COALESCE((
                       SELECT json_group_array(tag) FROM (
                           SELECT tag FROM prospect_tags
                           WHERE candidate_id=c.id ORDER BY tag
                       )
                   ), '[]') AS tags_json,
                   COALESCE((
                       SELECT COUNT(*) FROM prospect_activities pa
                       WHERE pa.candidate_id=c.id
                         AND pa.activity_type IN (
                             'email_sent', 'linkedin_message', 'call', 'outreach_recorded'
                         )
                   ), 0) AS contact_count,
                   (
                       SELECT MAX(pa.occurred_at) FROM prospect_activities pa
                       WHERE pa.candidate_id=c.id
                         AND pa.activity_type IN (
                             'email_sent', 'linkedin_message', 'call', 'outreach_recorded'
                         )
                   ) AS last_contacted_at,
                   EXISTS(
                       SELECT 1 FROM changes ch
                       WHERE ch.candidate_id=c.id AND ch.run_id=s.run_id
                         AND ch.kind<>'new_candidate'
                   ) AS changed
            {joins}
            WHERE {where}
            ORDER BY starred DESC, {order_expression} DESC, s.final_score DESC,
                     s.observed_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        items = [self._prospect_dict(row) for row in rows]
        reliability_by_candidate = self.candidate_scan_reliability_bulk(
            int(item["candidate_id"]) for item in items
        )
        for item in items:
            item.update(reliability_by_candidate[int(item["candidate_id"])])
        return {"items": items, "total": int(total)}

    @staticmethod
    def _target_list_dict(row: sqlite3.Row, *, rank: int) -> dict[str, Any]:
        observation = SiteObservation.model_validate_json(row["observation_json"])
        ai = AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None
        target = LeadStore._target_intelligence_from_row(row)
        if target is None:  # pragma: no cover - the target query requires a projection
            raise RuntimeError("Hydrated target row is missing its projection")
        company_name = concise_company_name(
            ai_name=ai.company_name if ai else "",
            label_hint=row["label_hint"] or "",
            title=observation.title,
            canonical_domain=target["canonical_domain"],
            hostname=row["hostname"],
        )
        if ai and ai.company_url:
            company_url = ai.company_url
        elif target["canonical_domain"] != row["hostname"].removeprefix("www."):
            company_url = f"https://{target['canonical_domain']}"
        else:
            company_url = observation.final_url or f"https://{row['hostname']}"
        preferred_email = observation.contacts.preferred_email
        best_contact = next(
            (
                contact.model_dump(mode="json")
                for contact in observation.contacts.emails
                if contact.address == preferred_email
            ),
            None,
        )
        preferred_phone = observation.contacts.preferred_phone
        best_phone = next(
            (
                phone.model_dump(mode="json")
                for phone in observation.contacts.phones
                if phone.dial_number == preferred_phone
            ),
            None,
        )
        public_emails = [
            contact.address
            for contact in observation.contacts.emails
            if contact.outreach_eligible and not contact.historical
        ]
        aliases = json.loads(row["identity_aliases_json"] or "[]")
        target.update(
            {
                "candidate_id": int(row["candidate_id"]),
                "observed_at": row["observed_at"],
                "rank": rank,
                "alias_count": len(aliases),
                "identity_aliases": aliases,
            }
        )
        return {
            "rank": rank,
            "candidate_id": int(row["candidate_id"]),
            "hostname": row["hostname"],
            "source_apex": row["source_apex"],
            "first_seen": row["first_seen"],
            "company_name": company_name,
            "company_url": company_url,
            "canonical_domain": target["canonical_domain"],
            "canonical_host": target["canonical_domain"],
            "observed_at": row["observed_at"],
            "relationship_lane": target["relationship_lane"],
            "target_scope": target["target_scope"],
            "archetype": target["archetype"],
            "target_score": target["target_score"],
            "priority_score": target["priority_score"],
            "durable_fit_score": target["durable_fit_score"],
            "contact_now_score": target["contact_now_score"],
            "founder_priority_score": target["founder_priority_score"],
            "recommended_action": target["recommended_action"],
            "next_best_scan": target["next_best_scan"],
            "principal_signals_met": target["principal_signals_met"],
            "review_ready": target["review_ready"],
            "data_confidence": target["data_confidence"],
            "hydration_status": target["hydration_status"],
            "starred": bool(row["starred"]),
            "excluded": False,
            "scan_enabled": bool(row["scan_enabled"]),
            "scan_attempt_count": int(row["scan_attempt_count"] or 0),
            "scan_failure_count": int(row["scan_failure_count"] or 0),
            "scan_failure_rate": round(
                int(row["scan_failure_count"] or 0)
                / int(row["scan_attempt_count"] or 1)
                * 100,
                1,
            )
            if int(row["scan_attempt_count"] or 0)
            else 0.0,
            "notes": row["notes"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "discovery_evidence_urls": json.loads(row["discovery_evidence_json"] or "[]"),
            "provider_record_ids": json.loads(row["provider_record_ids_json"] or "[]"),
            "emails": public_emails,
            "contacts": observation.contacts.model_dump(mode="json"),
            "best_contact": best_contact,
            "best_phone": best_phone,
            "contact_urls": observation.contact_urls,
            "target_intelligence": target,
        }

    def ai_review_target_hosts(self, *, min_score: int) -> list[str]:
        """Return the complete ranked host projection for AI review planning."""
        return self.list_targets(
            lane="all", scope="all", min_score=min_score, include_total=False,
            include_aliases=False, include_ad_intelligence=False,
            include_reliability=False, _ai_hosts_only=True,
        )["hosts"]

    def ai_review_target_priorities(self, hosts: list[str], *, min_score: int) -> dict[str, int]:
        """Order eligible targets from their saved score and exact capacity projection."""
        priorities = {}
        for start in range(0, len(hosts), 250):
            batch = hosts[start:start + 250]
            marks = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                "SELECT c.hostname,c.discovery_rank,s.score_json,ti.target_score "
                "FROM candidates c JOIN snapshots s ON s.id=(SELECT MAX(newer.id) "
                "FROM snapshots newer WHERE newer.candidate_id=c.id) "
                "JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
                f"WHERE c.hostname IN ({marks}) AND ti.live=1 AND ti.disqualified=0 "
                "AND ti.target_score>=? AND ti.hypothesis_json!=''",
                [*batch, min_score],
            )
            for row in rows:
                score = ScoreCard.model_validate_json(row["score_json"])
                if not score.disqualified:
                    priorities[row["hostname"]] = max(
                        score.placement.score, score.contact_priority, score.total,
                        row["target_score"], round((row["discovery_rank"] or 0) * 2),
                    )
        return priorities

    def list_targets(
        self,
        *,
        lane: str = "buyer",
        scope: str = "direct",
        min_score: int = 0,
        min_principal_signals: int = 0,
        review_ready: bool | None = None,
        search: str = "",
        archetype: str = "",
        sort: str = "priority",
        limit: int = 100,
        offset: int = 0,
        include_total: bool = True,
        include_aliases: bool = True,
        include_ad_intelligence: bool = True,
        include_reliability: bool = True,
        _ai_hosts_only: bool = False,
    ) -> dict[str, Any]:
        """Return current hydrated companies ordered by transparent target evidence.

        The overview can disable supplementary metadata that is not rendered there. Keeping
        those options here avoids maintaining a second target projection query while retaining
        the complete response for the full Prospects view.
        """
        sort_fields = {
            "priority": "founder_priority_score",
            "durable_fit": "durable_fit_score",
            "contact_now": "contact_now_score",
            "target_score": "target_score",
        }
        if lane not in {*TARGET_RELATIONSHIP_LANES, "all"}:
            raise ValueError("lane must be buyer, referral_partner, or all")
        if scope not in {*TARGET_SCOPES, "all", ""}:
            raise ValueError("scope must be direct, adjacency, unclassified, out_of_scope, or all")
        if sort not in {*sort_fields, "failure_rate"}:
            raise ValueError(
                "sort must be priority, durable_fit, contact_now, target_score, or failure_rate"
            )
        min_score = max(0, min(int(min_score), 100))
        min_principal_signals = max(0, min(int(min_principal_signals), 3))
        limit = max(1, min(int(limit), 1_000))
        offset = max(0, int(offset))
        include_reliability = include_reliability or sort == "failure_rate"
        conditions = [
            "ti.live=1",
            "ti.disqualified=0",
            "target_is_suppressed(c.source_apex, c.hostname, ti.canonical_domain)=0",
            "COALESCE(w.excluded, 0)=0",
            "ti.target_score>=?",
            "ti.principal_signals_met>=?",
        ]
        params: list[Any] = [min_score, min_principal_signals]
        if lane != "all":
            conditions.append("ti.relationship_lane=?")
            params.append(lane)
        if scope not in {"", "all"}:
            conditions.append("ti.target_scope=?")
            params.append(scope)
        if review_ready is not None:
            conditions.append("ti.review_ready=?")
            params.append(int(review_ready))
        if search:
            conditions.append(
                "(lower(c.hostname) LIKE ? OR lower(ti.canonical_domain) LIKE ? "
                "OR lower(COALESCE(json_extract(s.observation_json, '$.title'), '')) LIKE ?)"
            )
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle, needle])
        if archetype:
            conditions.append("ti.archetype=?")
            params.append(archetype)
        where = " AND ".join(conditions)
        reliability_cte = """
            WITH reliability AS (
                SELECT candidate_id,
                       COUNT(*) AS scan_attempt_count,
                       SUM(CASE WHEN status IN (
                           'transient_error', 'rate_limited', 'robots_blocked',
                           'identity_ambiguous', 'failed'
                       ) THEN 1 ELSE 0 END) AS scan_failure_count
                FROM scanner_executions
                WHERE candidate_id IS NOT NULL AND status IN (
                    'success', 'success_empty', 'partial', 'transient_error',
                    'rate_limited', 'robots_blocked', 'identity_ambiguous', 'failed'
                )
                GROUP BY candidate_id
            ),
        """ if include_reliability else "WITH "
        reliability_join = (
            "LEFT JOIN reliability r ON r.candidate_id=c.id" if include_reliability else ""
        )
        reliability_select = (
            "COALESCE(r.scan_attempt_count, 0) AS scan_attempt_count, "
            "COALESCE(r.scan_failure_count, 0) AS scan_failure_count,"
            if include_reliability
            else "0 AS scan_attempt_count, 0 AS scan_failure_count,"
        )
        aliases_cte = ""
        aliases_join = ""
        aliases_select = "'[]' AS identity_aliases_json"
        if include_aliases:
            aliases_cte = """
            , current_aliases AS MATERIALIZED (
                SELECT ti.canonical_domain,
                       c.id AS candidate_id,
                       c.hostname,
                       c.source_apex
                FROM latest l
                JOIN snapshot_target_intelligence ti ON ti.snapshot_id=l.snapshot_id
                JOIN candidates c ON c.id=l.candidate_id
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                WHERE COALESCE(w.excluded, 0)=0
                ORDER BY c.id
            ), aliases AS MATERIALIZED (
                SELECT canonical_domain,
                       json_group_array(json_object(
                           'candidate_id', candidate_id,
                           'hostname', hostname,
                           'source_apex', source_apex
                       )) AS identity_aliases_json
                FROM current_aliases
                GROUP BY canonical_domain
            )
            """
            aliases_join = "LEFT JOIN aliases a ON a.canonical_domain=e.target_canonical_domain"
            aliases_select = "COALESCE(a.identity_aliases_json, '[]') AS identity_aliases_json"
        identity_score_field = sort_fields.get(sort, sort_fields["priority"])
        identity_score_order = f"ti.{identity_score_field} DESC,"
        projection = """
            ti.founder_priority_score AS target_founder_priority_score,
            ti.contact_now_score AS target_contact_now_score,
            ti.durable_fit_score AS target_durable_fit_score,
            ti.review_ready AS target_review_ready, ti.target_score,
            ti.principal_signals_met AS target_principal_signals_met,
            ti.data_confidence AS target_data_confidence,
            (ti.hypothesis_json IS NOT NULL AND ti.hypothesis_json != '') AS has_hypothesis,
        """ if _ai_hosts_only else f"""
            c.source_apex, c.first_seen, c.label_hint, c.discovery_evidence_json,
            c.provider_record_ids_json, s.id AS snapshot_id, s.observation_json, s.ai_json,
            {TARGET_PROJECTION_SELECT},
            COALESCE(w.starred, 0) AS starred,
            COALESCE(w.scan_enabled, 1) AS scan_enabled,
            COALESCE(w.notes, '') AS notes,
            {reliability_select}
            COALESCE((
                SELECT json_group_array(tag) FROM (
                    SELECT tag FROM prospect_tags WHERE candidate_id=c.id ORDER BY tag
                )
            ), '[]') AS tags_json,
        """
        latest = """
            latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id FROM snapshots GROUP BY candidate_id
            ),
        """
        target_joins = """
            FROM latest l
            JOIN candidates c ON c.id=l.candidate_id
            JOIN snapshots s ON s.id=l.snapshot_id
            JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
        """
        if _ai_hosts_only:
            # Use the covering index before reading large snapshot or hypothesis rows.
            latest = """
                eligible_ids AS MATERIALIZED (
                    SELECT snapshot_id
                    FROM snapshot_target_intelligence INDEXED BY idx_snapshot_target_ranking
                    WHERE live=1 AND disqualified=0 AND target_score>=?
                      AND principal_signals_met>=?
                ),
            """
            target_joins = """
                FROM eligible_ids l
                CROSS JOIN snapshot_target_intelligence ti ON ti.snapshot_id=l.snapshot_id
                CROSS JOIN snapshots s ON s.id=l.snapshot_id
                CROSS JOIN candidates c ON c.id=s.candidate_id
            """
            where += (
                " AND s.id=(SELECT MAX(newer.id) FROM snapshots newer "
                "WHERE newer.candidate_id=c.id)"
            )
            params = [min_score, min_principal_signals, *params]
        cte = f"""
            {reliability_cte}{latest} eligible AS (
                SELECT c.id AS candidate_id, c.hostname, c.discovery_rank, s.observed_at,
                       {projection}
                       ROW_NUMBER() OVER (
                           PARTITION BY ti.canonical_domain
                           ORDER BY {identity_score_order}
                                    ti.founder_priority_score DESC,
                                    ti.contact_now_score DESC,
                                    ti.durable_fit_score DESC,
                                    ti.review_ready DESC, ti.target_score DESC,
                                    ti.principal_signals_met DESC,
                                    ti.data_confidence DESC, s.observed_at DESC,
                                    c.discovery_rank DESC, c.id DESC
                       ) AS identity_rank
                {target_joins}
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                {reliability_join}
                WHERE {where}
            ){aliases_cte}
        """
        total = None
        if include_total:
            total = int(
                self.connection.execute(
                    f"{cte} SELECT COUNT(*) FROM eligible WHERE identity_rank=1",
                    params,
                ).fetchone()[0]
            )
        reliability_order = """
            CASE WHEN e.scan_attempt_count>0
                 THEN 1.0 * e.scan_failure_count / e.scan_attempt_count
                 ELSE -1 END DESC,
            e.scan_attempt_count DESC,
        """
        score_order_fields = {
            "priority": "e.target_founder_priority_score",
            "durable_fit": "e.target_durable_fit_score",
            "contact_now": "e.target_contact_now_score",
            "target_score": "e.target_score",
        }
        default_order = f"""
            {score_order_fields.get(sort, score_order_fields['priority'])} DESC,
            e.target_founder_priority_score DESC,
            e.target_contact_now_score DESC,
            e.target_durable_fit_score DESC,
            e.target_review_ready DESC, e.target_score DESC,
            e.target_principal_signals_met DESC,
            e.target_data_confidence DESC,
        """
        order = reliability_order if sort == "failure_rate" else default_order
        if _ai_hosts_only:
            # Every stored hypothesis includes the manual criteria gaps.
            rows = self.connection.execute(
                f"""{cte}
                SELECT e.hostname FROM eligible e WHERE e.identity_rank=1 AND e.has_hypothesis
                ORDER BY {order} e.observed_at DESC, e.discovery_rank DESC, e.candidate_id DESC
                """,
                params,
            )
            return {"hosts": [str(row["hostname"]) for row in rows]}
        rows = self.connection.execute(
            f"""
            {cte}
            SELECT e.*,
                   {aliases_select}
            FROM eligible e
            {aliases_join}
            WHERE e.identity_rank=1
            ORDER BY {order} e.observed_at DESC,
                     e.discovery_rank DESC, e.candidate_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        items = [
            self._target_list_dict(row, rank=offset + index + 1) for index, row in enumerate(rows)
        ]
        if not include_reliability and items:
            reliability_by_candidate = self.candidate_scan_reliability_bulk(
                int(item["candidate_id"]) for item in items
            )
            for item in items:
                item.update(
                    reliability_by_candidate.get(
                        int(item["candidate_id"]),
                        {
                            "scan_attempt_count": 0,
                            "scan_failure_count": 0,
                            "scan_failure_rate": 0.0,
                            "scan_failure_by_type": [],
                        },
                    )
                )
        if include_ad_intelligence:
            for item in items:
                item["ad_intelligence"] = self.current_candidate_ad_intelligence(
                    int(item["candidate_id"])
                )
        else:
            for item in items:
                item["ad_intelligence"] = {}
        return {"items": items, "total": total}

    def target_data_generation(self) -> str:
        """Return a monotonic-enough token for invalidating target read caches."""

        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(snapshot_id), 0) AS snapshot_id,
                   COALESCE(MAX(computed_at), '') AS computed_at,
                   COUNT(*) AS projection_count
            FROM snapshot_target_intelligence
            """
        ).fetchone()
        return f"{int(row['snapshot_id'])}:{int(row['projection_count'])}:{row['computed_at']}"

    def treg_people_ranking_candidates(
        self,
        *,
        limit: int = 2_500,
        as_of: datetime | None = None,
        due_only: bool = False,
        candidate_ids: set[int] | None = None,
        include_target: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the ranked people cohort, optionally taking only due eligible companies."""

        bounded = max(1, min(int(limit), 2_500))
        now = self._utc_datetime(as_of or datetime.now(UTC))
        from .sales_engine.research_allocation import canonical_provider_candidates

        current = canonical_provider_candidates(
            self, provider="people", limit=bounded, now=now, due_only=due_only,
            candidate_ids=candidate_ids, include_target=include_target,
        )
        if current is not None:
            return current
        freshness_cutoff = now - timedelta(days=TREG_PEOPLE_REFRESH_DAYS)
        allowed = json.dumps(sorted(candidate_ids)) if candidate_ids is not None else None
        rows = self.connection.execute(
            """
            WITH last_people AS (
                SELECT execution_id, company_key, status, gathered_at,
                       observation_count
                FROM (
                    SELECT id AS execution_id, company_key, status,
                           COALESCE(finished_at, started_at) AS gathered_at,
                           observation_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY company_key
                               ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
                           ) AS recency_rank
                    FROM scanner_executions
                    WHERE scanner_id=? AND scanner_version=?
                      AND status IN ('success', 'success_empty')
                      AND recorded_at<=? AND started_at<=?
                      AND COALESCE(finished_at, started_at)<=?
                ) WHERE recency_rank=1
            ), latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            ), eligible AS (
                SELECT c.id AS candidate_id, c.hostname,
                       ti.canonical_domain, ti.founder_priority_score,
                       ti.contact_now_score, ti.durable_fit_score,
                       ti.review_ready, ti.target_score,
                       ti.principal_signals_met, ti.data_confidence,
                       COALESCE(json_extract(s.score_json, '$.archetype'), 'unknown')
                           AS archetype,
                       s.observed_at, c.discovery_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY ti.canonical_domain
                           ORDER BY ti.founder_priority_score DESC,
                                    ti.contact_now_score DESC,
                                    ti.durable_fit_score DESC,
                                    ti.review_ready DESC, ti.target_score DESC,
                                    ti.principal_signals_met DESC,
                                    ti.data_confidence DESC, s.observed_at DESC,
                                    c.discovery_rank DESC, c.id DESC
                       ) AS identity_rank
                FROM latest l
                JOIN candidates c ON c.id=l.candidate_id
                JOIN snapshots s ON s.id=l.snapshot_id
                JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                WHERE ti.live=1 AND ti.disqualified=0
                  AND target_is_suppressed(
                      c.source_apex, c.hostname, ti.canonical_domain
                  )=0
                  AND COALESCE(w.excluded, 0)=0
            )
            SELECT candidate_id, hostname, canonical_domain, archetype,
                   founder_priority_score, contact_now_score,
                   durable_fit_score, target_score,
                   last_people.execution_id AS people_execution_id,
                   last_people.status AS people_status,
                   last_people.gathered_at AS last_people_gathered_at,
                   COALESCE(last_people.observation_count, 0) AS people_good_contact_count
            FROM eligible
            LEFT JOIN last_people ON last_people.company_key=eligible.canonical_domain
            WHERE identity_rank=1
              AND (?=0 OR last_people.gathered_at IS NULL OR last_people.gathered_at<=?)
              AND (? IS NULL OR candidate_id IN (SELECT value FROM json_each(?)))
            ORDER BY founder_priority_score DESC, contact_now_score DESC,
                     durable_fit_score DESC, review_ready DESC,
                     target_score DESC, principal_signals_met DESC,
                     data_confidence DESC, observed_at DESC,
                     discovery_rank DESC, candidate_id DESC
            LIMIT ?
            """,
            (
                TREG_PEOPLE_SCANNER_ID,
                TREG_PEOPLE_SCANNER_VERSION,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
                int(due_only),
                freshness_cutoff.isoformat(),
                allowed,
                allowed,
                bounded,
            ),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            last_gathered = self._datetime_or_none(row["last_people_gathered_at"])
            good_contact_count = int(row["people_good_contact_count"] or 0)
            people_current = (
                last_gathered is not None and last_gathered > freshness_cutoff
            )
            next_due = (
                last_gathered + timedelta(days=TREG_PEOPLE_REFRESH_DAYS)
                if last_gathered is not None
                else None
            )
            items.append(
                {
                    "rank": index,
                    "candidate_id": int(row["candidate_id"]),
                    "hostname": str(row["hostname"]),
                    "canonical_domain": str(row["canonical_domain"]),
                    "archetype": str(row["archetype"] or "unknown"),
                    "founder_priority_score": int(row["founder_priority_score"] or 0),
                    "contact_now_score": int(row["contact_now_score"] or 0),
                    "durable_fit_score": int(row["durable_fit_score"] or 0),
                    "target_score": int(row["target_score"] or 0),
                    "people_execution_id": (
                        int(row["people_execution_id"])
                        if row["people_execution_id"] is not None
                        else None
                    ),
                    "people_status": row["people_status"],
                    "good_contact_count": good_contact_count,
                    "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
                    "last_gathered_at": (
                        last_gathered.isoformat() if last_gathered is not None else None
                    ),
                    "next_due_at": next_due.isoformat() if next_due is not None else None,
                    "people_current": people_current,
                    "people_due": not people_current,
                }
            )
        return items

    def treg_people_hydration_status(
        self,
        *,
        limit: int = 2_500,
        as_of: datetime | None = None,
        coverage_only: bool = False,
    ) -> dict[str, Any]:
        """Summarize current ranked-cohort people coverage without exposing provider payloads."""

        as_of = self._utc_datetime(as_of or datetime.now(UTC))
        cohort = self.treg_people_ranking_candidates(
            limit=limit, as_of=as_of, include_target=False,
        )
        fresh = sum(not bool(item["people_due"]) for item in cohort)
        thin = sum(
            bool(item["people_current"])
            and int(item["good_contact_count"]) < TREG_PEOPLE_MIN_GOOD_CONTACTS
            for item in cohort
        )
        summary = {
            "cohort_limit": max(1, min(int(limit), 2_500)),
            "cohort_count": len(cohort),
            "fresh_companies": fresh,
            "thin_companies": thin,
            "due_companies": len(cohort) - fresh,
            "refresh_days": TREG_PEOPLE_REFRESH_DAYS,
            "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
        }
        if coverage_only:
            return summary
        execution_ids = [
            int(item["people_execution_id"])
            for item in cohort
            if item.get("people_execution_id") is not None and item["people_current"]
        ]
        now = as_of.isoformat()
        people_count = 0
        verified_email_count = 0
        for start in range(0, len(execution_ids), 500):
            batch = execution_ids[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            row = self.connection.execute(
                f"""
                SELECT COUNT(*) AS people_count,
                       COALESCE(SUM(
                           CASE WHEN COALESCE((
                               SELECT CASE WHEN el.status='found' AND el.email_verified=0
                                      THEN 'unverified' ELSE el.status END
                               FROM company_email_lookups el
                               WHERE el.company_key=ao.company_key
                                 AND el.person_key=COALESCE(
                                     NULLIF(rtrim(lower(json_extract(
                                         ao.value_json, '$.linkedin_url')), '/'), ''),
                                     lower(COALESCE(json_extract(ao.value_json, '$.first_name'), '')
                                         || '|' || COALESCE(json_extract(
                                             ao.value_json, '$.last_name'), '')
                                         || '|' || COALESCE(json_extract(
                                             ao.value_json, '$.title'), '')))
                                 AND el.observed_at<=? AND el.expires_at>?
                                 AND el.status IN ('found', 'not_found')
                               ORDER BY el.id DESC LIMIT 1
                           ), CASE WHEN json_extract(value_json, '$.email_verification_policy')
                               ='mailbox-checked-v1' THEN json_extract(value_json, '$.email_status')
                               ELSE 'unverified' END)='found'
                                THEN 1 ELSE 0 END
                       ), 0) AS verified_email_count
                FROM atomic_observations ao
                WHERE execution_id IN ({placeholders})
                  AND scanner_id=?
                  AND predicate='contact.provider_professional_person'
                """,
                [now, now, *batch, TREG_PEOPLE_SCANNER_ID],
            ).fetchone()
            people_count += int(row["people_count"] or 0)
            verified_email_count += int(row["verified_email_count"] or 0)
        return {
            **summary,
            "people_count": people_count,
            "verified_email_count": verified_email_count,
        }

    def treg_people_intelligence(
        self,
        company_key: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Project the latest normalized Treg people result, separate from public contacts."""

        company = self._normalized_company_key(company_key)
        now = self._utc_datetime(as_of or datetime.now(UTC))
        row = self.connection.execute(
            """
            SELECT * FROM scanner_executions
            WHERE company_key=? AND scanner_id=? AND scanner_version=?
              AND status IN ('success', 'success_empty')
              AND recorded_at<=? AND started_at<=?
              AND COALESCE(finished_at, started_at)<=?
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
            LIMIT 1
            """,
            (
                company,
                TREG_PEOPLE_SCANNER_ID,
                TREG_PEOPLE_SCANNER_VERSION,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        ).fetchone()
        people: list[dict[str, Any]] = []
        scanned_at: datetime | None = None
        expires_at: datetime | None = None
        status = "never_scanned"
        if row is not None:
            scanned_at = self._datetime_or_none(row["finished_at"] or row["started_at"])
            expires_at = (
                scanned_at + timedelta(days=TREG_PEOPLE_REFRESH_DAYS)
                if scanned_at is not None
                else None
            )
            current = expires_at is not None and expires_at > now
            good_contact_count = int(row["observation_count"] or 0)
            status = (
                "fresh"
                if current and good_contact_count >= TREG_PEOPLE_MIN_GOOD_CONTACTS
                else "needs_more_contacts"
                if current
                else "stale"
            )
            if current:
                observation_rows = self.connection.execute(
                    """
                    SELECT * FROM atomic_observations
                    WHERE execution_id=? AND scanner_id=?
                      AND predicate='contact.provider_professional_person'
                      AND first_observed_at<=?
                      AND (expires_at IS NULL OR expires_at>?)
                    ORDER BY json_extract(value_json, '$.role_score') DESC, id
                    """,
                    (
                        int(row["id"]),
                        TREG_PEOPLE_SCANNER_ID,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                ).fetchall()
                for observation_row in observation_rows:
                    observation = self._atomic_observation_from_row(observation_row)
                    if isinstance(observation.value, dict):
                        people.append(dict(observation.value))
        from .email_lookup import overlay_email_results

        overlay_email_results(self, company, people, now=now)
        return {
            "source_kind": "licensed_professional_provider",
            "provider": "Treg",
            "search_provider": "IcyPeas",
            "scanner_id": TREG_PEOPLE_SCANNER_ID,
            "status": status,
            "refresh_days": TREG_PEOPLE_REFRESH_DAYS,
            "minimum_good_contacts": TREG_PEOPLE_MIN_GOOD_CONTACTS,
            "good_contact_count": len(people),
            "scanned_at": scanned_at.isoformat() if scanned_at is not None else None,
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
            "people": people,
            "verified_email_count": sum(
                person.get("email_status") == "found" for person in people
            ),
            "disclaimer": (
                "Licensed professional data from Treg and its provider. A provider-verified "
                "work email does not establish permission or consent to contact."
            ),
        }

    def dataforseo_ranked_company_keys(self, *, limit=2_500, as_of=None) -> set[str]:
        """Exact SEO admission identities; no unused target JSON or receipt hydration."""
        from .sales_engine.research_allocation import canonical_provider_company_keys

        now = self._utc_datetime(as_of or datetime.now(UTC))
        keys = canonical_provider_company_keys(self, "seo", limit=limit, now=now)
        if keys is not None:
            return keys
        return {str(item["canonical_domain"]).strip().lower().removeprefix("www.")
                for item in self.dataforseo_ranking_candidates(limit=limit, as_of=now)}

    def dataforseo_ranking_candidates(
        self,
        *,
        limit: int = 2_500,
        as_of: datetime | None = None,
        exclude_roots=(),
        only_roots=None,
        bulk_selection: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ranked root representatives with durable monthly SEO eligibility.

        Explicit bulk plans filter registered roots before taking their authorized limit.
        Other callers retain the ordinary 2,500-root read ceiling and freshness fields.
        """

        bounded = max(1, min(int(limit), 100_000 if bulk_selection else 2_500))
        now = self._utc_datetime(as_of or datetime.now(UTC))
        from .sales_engine.research_allocation import canonical_provider_candidates

        current = canonical_provider_candidates(
            self, provider="seo", limit=bounded, now=now,
            exclude_roots=exclude_roots, only_roots=only_roots, bulk_selection=bulk_selection,
        )
        if current is not None:
            return current
        freshness_cutoff = now - timedelta(days=DATAFORSEO_REFRESH_DAYS)
        rows = self.connection.execute(
            """
            WITH last_seo AS (
                SELECT seo_registered_domain(company_key) AS company_key,
                       MAX(COALESCE(finished_at, started_at)) AS last_gathered_at
                FROM scanner_executions
                WHERE scanner_id=?
                  AND status IN ('success', 'partial')
                  AND recorded_at<=?
                  AND started_at<=?
                  AND COALESCE(finished_at, started_at)<=?
                  AND (
                      scope_key GLOB 'market_source:dataforseo:*:bulk'
                      OR scope_key GLOB 'market_source:dataforseo:*:domain_overview'
                  )
                GROUP BY seo_registered_domain(company_key)
            ), latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            ), eligible AS (
                SELECT c.id AS candidate_id, c.hostname,
                       ti.canonical_domain, ti.founder_priority_score,
                       ti.contact_now_score, ti.durable_fit_score,
                       ti.review_ready, ti.target_score,
                       ti.principal_signals_met, ti.data_confidence,
                       s.observed_at, c.discovery_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY seo_registered_domain(ti.canonical_domain)
                           ORDER BY ti.founder_priority_score DESC,
                                    ti.contact_now_score DESC,
                                    ti.durable_fit_score DESC,
                                    ti.review_ready DESC, ti.target_score DESC,
                                    ti.principal_signals_met DESC,
                                    ti.data_confidence DESC, s.observed_at DESC,
                                    c.discovery_rank DESC, c.id DESC
                       ) AS identity_rank
                FROM latest l
                JOIN candidates c ON c.id=l.candidate_id
                JOIN snapshots s ON s.id=l.snapshot_id
                JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                WHERE ti.live=1 AND ti.disqualified=0
                  AND seo_registered_domain(ti.canonical_domain)!=''
                  AND target_is_suppressed(
                      c.source_apex, c.hostname, ti.canonical_domain
                  )=0
                  AND COALESCE(w.excluded, 0)=0
            )
            SELECT candidate_id, hostname, canonical_domain,
                   seo_registered_domain(canonical_domain) AS seo_domain,
                   founder_priority_score, contact_now_score,
                   durable_fit_score, target_score, last_seo.last_gathered_at
            FROM eligible
            LEFT JOIN last_seo
                ON last_seo.company_key=seo_registered_domain(eligible.canonical_domain)
            WHERE identity_rank=1
              AND seo_registered_domain(canonical_domain) NOT IN (SELECT value FROM json_each(?))
              AND (? IS NULL OR seo_registered_domain(canonical_domain) IN (
                   SELECT value FROM json_each(?)
              ))
            ORDER BY founder_priority_score DESC, contact_now_score DESC,
                     durable_fit_score DESC, review_ready DESC,
                     target_score DESC, principal_signals_met DESC,
                     data_confidence DESC, observed_at DESC,
                     discovery_rank DESC, candidate_id DESC
            LIMIT ?
            """,
            (
                DATAFORSEO_SCANNER_ID,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
                json.dumps(sorted(exclude_roots)),
                json.dumps(sorted(only_roots)) if only_roots is not None else None,
                json.dumps(sorted(only_roots)) if only_roots is not None else None,
                bounded,
            ),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            last_gathered = self._datetime_or_none(row["last_gathered_at"])
            next_due = (
                last_gathered + timedelta(days=DATAFORSEO_REFRESH_DAYS)
                if last_gathered is not None
                else None
            )
            items.append(
                {
                    "rank": index,
                    "candidate_id": int(row["candidate_id"]),
                    "hostname": str(row["hostname"]),
                    "canonical_domain": str(row["canonical_domain"]),
                    "seo_domain": str(row["seo_domain"]),
                    "founder_priority_score": int(row["founder_priority_score"] or 0),
                    "contact_now_score": int(row["contact_now_score"] or 0),
                    "durable_fit_score": int(row["durable_fit_score"] or 0),
                    "target_score": int(row["target_score"] or 0),
                    "last_gathered_at": (
                        last_gathered.isoformat() if last_gathered is not None else None
                    ),
                    "next_due_at": next_due.isoformat() if next_due is not None else None,
                    "seo_due": last_gathered is None or last_gathered <= freshness_cutoff,
                }
            )
        return items

    def dataforseo_root_last_gathered(
        self, domains: list[str], *, as_of: datetime,
    ) -> dict[str, str]:
        """Share successful SEO clocks without rewriting historical subdomain receipts."""
        roots = list(dict.fromkeys(filter(None, (seo_registered_domain(d) for d in domains))))
        gathered: dict[str, str] = {}
        timestamp = self._utc_datetime(as_of).isoformat()
        for start in range(0, len(roots), 500):
            batch = roots[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                "SELECT seo_registered_domain(company_key) AS root,"
                "MAX(COALESCE(finished_at,started_at)) AS gathered_at FROM scanner_executions "
                f"WHERE seo_registered_domain(company_key) IN ({placeholders}) "
                "AND scanner_id=? AND status IN ('success','partial') "
                "AND recorded_at<=? AND started_at<=? AND COALESCE(finished_at,started_at)<=? "
                "AND (scope_key GLOB 'market_source:dataforseo:*:bulk' "
                "OR scope_key GLOB 'market_source:dataforseo:*:domain_overview') "
                "GROUP BY seo_registered_domain(company_key)",
                (*batch, DATAFORSEO_SCANNER_ID, timestamp, timestamp, timestamp),
            ).fetchall()
            gathered.update((row["root"], row["gathered_at"]) for row in rows)
        return gathered

    def dataforseo_run_completed_domains(self, smart_scan_id: str, *, stage: str) -> set[str]:
        """A committed root is complete even when its ranked representative changes."""
        if stage not in {"bulk", "domain_overview"}:
            raise ValueError("DataForSEO stage must be bulk or domain_overview")
        run = "".join(c for c in smart_scan_id.strip().lower() if c.isalnum())[:32]
        if not run:
            return set()
        rows = self.connection.execute(
            "SELECT DISTINCT seo_registered_domain(company_key) FROM scanner_executions "
            "WHERE scanner_id=? AND scope_key=? AND status IN ('success','partial')",
            (DATAFORSEO_SCANNER_ID, f"market_source:dataforseo:{run}:{stage}"),
        ).fetchall()
        return {row[0] for row in rows if row[0]}

    def dataforseo_run_completed_candidate_ids(
        self,
        smart_scan_id: str,
        *,
        stage: str,
    ) -> set[int]:
        """Return run-scoped vendor projections already committed before a retry/restart."""

        if stage not in {"bulk", "domain_overview"}:
            raise ValueError("DataForSEO stage must be bulk or domain_overview")
        normalized_run = "".join(
            character for character in smart_scan_id.strip().lower() if character.isalnum()
        )[:32]
        if not normalized_run:
            return set()
        scope_key = f"market_source:dataforseo:{normalized_run}:{stage}"
        rows = self.connection.execute(
            """
            SELECT DISTINCT candidate_id FROM scanner_executions
            WHERE scanner_id='dataforseo_search_visibility_v1'
              AND scope_key=? AND status IN ('success', 'partial')
              AND candidate_id IS NOT NULL
            """,
            (scope_key,),
        ).fetchall()
        return {int(row["candidate_id"]) for row in rows}

    def target_intelligence_status(self) -> dict[str, Any]:
        inventory = self.connection.execute(
            """
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            )
            SELECT (SELECT COUNT(*) FROM candidates) AS candidates,
                   (SELECT COUNT(*) FROM snapshots) AS historical_snapshots,
                   (SELECT COUNT(*) FROM snapshot_target_intelligence)
                       AS hydrated_historical_snapshots,
                   COUNT(l.candidate_id) AS inspected_candidates,
                   COUNT(ti.snapshot_id) AS hydrated_latest_candidates,
                   COALESCE(MAX(ti.target_score), 0) AS latest_maximum_score,
                   COALESCE(SUM(ti.target_score>=50), 0) AS latest_score_50_plus,
                   COALESCE(SUM(ti.target_score>=30), 0) AS latest_score_30_plus,
                   COALESCE(SUM(ti.review_ready), 0) AS latest_review_ready,
                   MAX(s.observed_at) AS latest_observed_at,
                   MAX(ti.computed_at) AS latest_computed_at
            FROM candidates c
            LEFT JOIN latest l ON l.candidate_id=c.id
            LEFT JOIN snapshots s ON s.id=l.snapshot_id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            """
        ).fetchone()
        eligible = self.connection.execute(
            """
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            ), current AS (
                SELECT ti.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY ti.relationship_lane, ti.target_scope,
                                        ti.canonical_domain
                           ORDER BY ti.review_ready DESC, ti.target_score DESC,
                                    ti.principal_signals_met DESC,
                                    ti.data_confidence DESC, s.observed_at DESC,
                                    ti.candidate_id DESC
                       ) AS identity_rank
                FROM latest l
                JOIN candidates c ON c.id=l.candidate_id
                JOIN snapshots s ON s.id=l.snapshot_id
                JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                LEFT JOIN prospect_workflow w ON w.candidate_id=ti.candidate_id
                WHERE ti.live=1 AND ti.disqualified=0
                  AND target_is_suppressed(
                      c.source_apex, c.hostname, ti.canonical_domain
                  )=0
                  AND COALESCE(w.excluded, 0)=0
            )
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(relationship_lane='buyer'), 0) AS buyers,
                   COALESCE(SUM(relationship_lane='referral_partner'), 0) AS referrals,
                   COALESCE(SUM(target_scope='direct'), 0) AS direct,
                   COALESCE(SUM(target_scope='adjacency'), 0) AS adjacency,
                   COALESCE(SUM(target_scope='unclassified'), 0) AS unclassified,
                   COALESCE(SUM(target_scope='out_of_scope'), 0) AS out_of_scope,
                   COALESCE(SUM(
                       relationship_lane='buyer' AND target_scope='direct'
                       AND target_score>=?
                   ), 0) AS at_threshold,
                   COALESCE(MAX(CASE
                       WHEN relationship_lane='buyer' AND target_scope='direct'
                       THEN target_score ELSE 0 END
                   ), 0) AS maximum_score,
                   COALESCE(SUM(
                       relationship_lane='buyer' AND target_scope='direct'
                       AND review_ready
                   ), 0) AS review_ready
            FROM current WHERE identity_rank=1
            """,
            (TARGET_SCORE_THRESHOLD,),
        ).fetchone()
        migration = self.connection.execute(
            """
            SELECT applied_at, rows_changed FROM schema_migrations WHERE name=?
            """,
            (TARGET_INTELLIGENCE_MIGRATION,),
        ).fetchone()
        candidate_count = int(inventory["candidates"] or 0)
        snapshot_count = int(inventory["historical_snapshots"] or 0)
        inspected_count = int(inventory["inspected_candidates"] or 0)
        hydrated_snapshot_count = int(inventory["hydrated_historical_snapshots"] or 0)
        hydrated_latest_count = int(inventory["hydrated_latest_candidates"] or 0)
        maximum_score = int(eligible["maximum_score"] or 0)
        return {
            "rubric_version": "creative-capacity-v3-provisional",
            "migration": {
                "name": TARGET_INTELLIGENCE_MIGRATION,
                "applied": migration is not None,
                "applied_at": migration["applied_at"] if migration else None,
                "rows_changed": int(migration["rows_changed"] or 0) if migration else 0,
            },
            "inventory": {
                "candidates": candidate_count,
                "inspected_candidates": inspected_count,
                "uninspected_candidates": max(candidate_count - inspected_count, 0),
                "historical_snapshots": snapshot_count,
                "hydrated_historical_snapshots": hydrated_snapshot_count,
                "projection_backlog": max(snapshot_count - hydrated_snapshot_count, 0),
                "hydrated_latest_candidates": hydrated_latest_count,
                "inventory_coverage_percent": (
                    round(inspected_count / candidate_count * 100, 2) if candidate_count else 0.0
                ),
                "projection_coverage_percent": (
                    round(hydrated_snapshot_count / snapshot_count * 100, 2)
                    if snapshot_count
                    else 100.0
                ),
            },
            "eligible_canonical_targets": int(eligible["total"] or 0),
            "lanes": {
                "buyer": int(eligible["buyers"] or 0),
                "referral_partner": int(eligible["referrals"] or 0),
            },
            "scopes": {
                "direct": int(eligible["direct"] or 0),
                "adjacency": int(eligible["adjacency"] or 0),
                "unclassified": int(eligible["unclassified"] or 0),
                "out_of_scope": int(eligible["out_of_scope"] or 0),
            },
            "threshold": {
                "target_score": TARGET_SCORE_THRESHOLD,
                "at_or_above": int(eligible["at_threshold"] or 0),
                "review_ready": int(eligible["review_ready"] or 0),
                "maximum_score": maximum_score,
                "shortfall": max(TARGET_SCORE_THRESHOLD - maximum_score, 0),
            },
            "distribution": {
                "maximum_score": int(inventory["latest_maximum_score"] or 0),
                "score_50_plus": int(inventory["latest_score_50_plus"] or 0),
                "score_30_plus": int(inventory["latest_score_30_plus"] or 0),
                "review_ready": int(inventory["latest_review_ready"] or 0),
            },
            "freshness": {
                "latest_observed_at": inventory["latest_observed_at"],
                "latest_computed_at": inventory["latest_computed_at"],
            },
        }

    def target_hydration_backlog(
        self,
        source_apexes: list[str],
        *,
        limit: int = 20_000,
    ) -> list[DiscoveredHost]:
        """Return persisted source inventory, unseen first, without deleting suppressed rows."""
        if not source_apexes or limit <= 0:
            return []
        unique_sources = list(dict.fromkeys(source_apexes))
        placeholders = ", ".join("?" for _ in unique_sources)
        fetch_limit = max(limit, min(limit * 4, 80_000))
        rows = self.connection.execute(
            f"""
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            )
            SELECT c.source_apex, c.hostname, c.first_seen, c.discovery_rank,
                   c.label_hint, c.discovery_evidence_json,
                   c.provider_record_ids_json, c.discovery_assertions_json,
                   s.observed_at,
                   ti.target_score, ti.review_ready, ti.canonical_domain
            FROM candidates c
            LEFT JOIN latest l ON l.candidate_id=c.id
            LEFT JOIN snapshots s ON s.id=l.snapshot_id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE c.source_apex IN ({placeholders})
            ORDER BY CASE WHEN l.snapshot_id IS NULL THEN 0 ELSE 1 END,
                     CASE WHEN s.observed_at IS NULL THEN '' ELSE s.observed_at END,
                     COALESCE(ti.review_ready, 0) DESC,
                     COALESCE(ti.target_score, 0) DESC,
                     c.discovery_rank DESC, c.last_discovered_at DESC, c.id DESC
            LIMIT ?
            """,
            [*unique_sources, fetch_limit],
        ).fetchall()
        backlog: list[DiscoveredHost] = []
        for row in rows:
            candidate = DiscoveredHost(
                source_apex=row["source_apex"],
                hostname=row["hostname"],
                first_seen=(
                    datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else None
                ),
                discovery_rank=float(row["discovery_rank"] or 0),
                label_hint=row["label_hint"] or "",
                discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
                provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
                discovery_assertions=json.loads(row["discovery_assertions_json"] or "[]"),
            )
            if _target_is_suppressed(
                candidate.source_apex,
                candidate.hostname,
                row["canonical_domain"],
            ):
                continue
            backlog.append(candidate)
            if len(backlog) >= limit:
                break
        return backlog

    def smart_scan_new_companies(
        self,
        *,
        started_at: str,
        start_candidate_id: int | None = None,
        finished_at: str | None = None,
        excluded_candidate_ids: list[int] | None = None,
    ) -> tuple[int, int]:
        """Count retained first admissions; never count refreshes or AUTOINCREMENT gaps."""
        if start_candidate_id is None:
            # One-time legacy recovery. Subsequent checkpoints use the primary-key range.
            first_new = self.connection.execute(
                "SELECT MIN(id) FROM candidates WHERE discovered_at >= ?",
                (started_at,),
            ).fetchone()[0]
            start_candidate_id = (
                int(first_new) - 1 if first_new is not None else int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM candidates"
                    ).fetchone()[0]
                )
            )
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM candidates
            WHERE id > ? AND discovered_at >= ?
              AND (? IS NULL OR discovered_at <= ?)
              AND id NOT IN (SELECT value FROM json_each(?))
            """,
            (
                start_candidate_id, started_at, finished_at, finished_at,
                json.dumps(excluded_candidate_ids or []),
            ),
        ).fetchone()
        return start_candidate_id, int(row[0])

    def save_smart_scan_run(self, state: dict[str, Any], *, commit: bool = True) -> None:
        scan_id = str(state.get("id") or "").strip()
        if not scan_id:
            raise ValueError("Smart Scan state requires an id")
        timestamp = str(state.get("updated_at") or _now())
        started_at = str(state.get("started_at") or timestamp)
        finished_at = state.get("finished_at")
        self.connection.execute(
            """
            INSERT INTO smart_scan_runs(
                id, status, control_status, state_json, started_at, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                control_status=excluded.control_status,
                state_json=excluded.state_json,
                updated_at=excluded.updated_at,
                finished_at=excluded.finished_at
            """,
            (
                scan_id,
                str(state.get("status") or "idle"),
                str(state.get("control_status") or "running"),
                json.dumps(state, separators=(",", ":")),
                started_at,
                timestamp,
                str(finished_at) if finished_at else None,
            ),
        )
        if commit:
            self.connection.commit()

    def latest_smart_scan_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_json FROM smart_scan_runs ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["state_json"]) if row is not None else None

    def clear_smart_scan_presentation(self, scan_id: str) -> None:
        """Dismiss a terminal run without deleting its observations or history."""
        row = self.connection.execute(
            "SELECT status, state_json FROM smart_scan_runs WHERE id=?", (scan_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Smart Scan history was not found")
        if row["status"] not in {"complete", "partial", "failed", "stopped"}:
            raise ValueError("Stop the active Smart Scan before clearing it")
        state = json.loads(row["state_json"])
        state.setdefault("cleared_at", _now())
        # Preserve chronology and the complete run record; only dismissal is new.
        self.connection.execute(
            "UPDATE smart_scan_runs SET state_json=? WHERE id=?",
            (json.dumps(state, separators=(",", ":")), scan_id),
        )
        self.connection.commit()

    def append_smart_scan_event(
        self,
        smart_scan_id: str,
        event: dict[str, Any],
        *,
        retain: int,
        commit: bool = True,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO smart_scan_events(
                smart_scan_id, sequence, timestamp, level, phase, message, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(smart_scan_id, sequence) DO UPDATE SET
                timestamp=excluded.timestamp,
                level=excluded.level,
                phase=excluded.phase,
                message=excluded.message,
                detail=excluded.detail
            """,
            (
                smart_scan_id,
                int(event.get("id") or 0),
                str(event.get("timestamp") or _now()),
                str(event.get("level") or "info"),
                str(event.get("phase") or "idle"),
                str(event.get("message") or ""),
                str(event.get("detail") or ""),
            ),
        )
        self.connection.execute(
            """
            DELETE FROM smart_scan_events
            WHERE smart_scan_id=? AND sequence <= (
                SELECT COALESCE(MAX(sequence), 0) - ?
                FROM smart_scan_events WHERE smart_scan_id=?
            )
            """,
            (smart_scan_id, max(1, int(retain)), smart_scan_id),
        )
        if commit:
            self.connection.commit()

    def smart_scan_events(
        self,
        smart_scan_id: str,
        *,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT sequence, timestamp, level, phase, message, detail
            FROM smart_scan_events
            WHERE smart_scan_id=? ORDER BY sequence DESC LIMIT ?
            """,
            (smart_scan_id, max(1, min(int(limit), 1_000))),
        ).fetchall()
        return [
            {
                "id": int(row["sequence"]),
                "timestamp": row["timestamp"],
                "level": row["level"],
                "phase": row["phase"],
                "message": row["message"],
                "detail": row["detail"],
            }
            for row in reversed(rows)
        ]

    def record_smart_scan_error(
        self,
        smart_scan_id: str,
        *,
        phase: str,
        message: str,
        detail: str,
        timestamp: str | None = None,
        commit: bool = True,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO smart_scan_errors(
                smart_scan_id, timestamp, phase, message, detail
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (smart_scan_id, timestamp or _now(), phase, message, detail),
        )
        if commit:
            self.connection.commit()
        return int(cursor.lastrowid)

    def smart_scan_errors(
        self,
        smart_scan_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        configure_error_recovery(self.connection)
        cutoff = (datetime.now(UTC) - timedelta(days=ERROR_LOG_RETENTION_DAYS)).isoformat()
        normalized_offset = max(0, int(offset))
        normalized_limit = max(1, min(int(limit), 500))
        total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM smart_scan_errors WHERE smart_scan_id=? "
                f"AND julianday(timestamp)>=julianday(?) AND {UNRESOLVED}",
                (smart_scan_id, cutoff),
            ).fetchone()[0]
        )
        recorded_total = self.connection.execute(
            "SELECT COUNT(*) FROM smart_scan_errors WHERE smart_scan_id=? "
            "AND julianday(timestamp)>=julianday(?)", (smart_scan_id, cutoff),
        ).fetchone()[0]
        rows = self.connection.execute(
            f"""
            SELECT id, timestamp, phase, message, detail
            FROM smart_scan_errors
            WHERE smart_scan_id=? AND julianday(timestamp)>=julianday(?) AND {UNRESOLVED}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (smart_scan_id, cutoff, normalized_limit, normalized_offset),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "resolved_total": recorded_total - total,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "has_more": normalized_offset + len(rows) < total,
        }

    @staticmethod
    def read_smart_scan_errors(
        path: str | Path,
        smart_scan_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        activity: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Read Smart Scan errors without running connection-time schema pragmas.

        This endpoint is polled while scan workers are writing heavily. Opening the normal
        ``LeadStore`` can wait on ``PRAGMA journal_mode`` even though a WAL reader itself would not
        conflict with the writer, so the error-tab read uses a short-lived query-only connection.
        """

        cutoff = (datetime.now(UTC) - timedelta(days=ERROR_LOG_RETENTION_DAYS)).isoformat()
        normalized_offset = max(0, int(offset))
        normalized_limit = max(1, min(int(limit), 500))
        database = Path(path).resolve()
        if not database.exists():
            return {
                "items": [],
                "total": 0,
                "offset": normalized_offset,
                "limit": normalized_limit,
                "has_more": False,
            }
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            configure_error_recovery(connection)
            connection.execute("BEGIN")
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM smart_scan_errors WHERE smart_scan_id=? "
                    f"AND julianday(timestamp)>=julianday(?) AND {UNRESOLVED}",
                    (smart_scan_id, cutoff),
                ).fetchone()[0]
            )
            recorded_total = connection.execute(
                "SELECT COUNT(*) FROM smart_scan_errors WHERE smart_scan_id=? "
                "AND julianday(timestamp)>=julianday(?)", (smart_scan_id, cutoff),
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT id, timestamp, phase, message, detail
                FROM smart_scan_errors
                WHERE smart_scan_id=? AND julianday(timestamp)>=julianday(?) AND {UNRESOLVED}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (smart_scan_id, cutoff, normalized_limit, normalized_offset),
            ).fetchall()
            from .error_recovery import resolved_activity

            signatures = resolved_activity(connection, activity or [])
        finally:
            connection.close()
        payload = {
            "items": [dict(row) for row in rows],
            "total": total,
            "resolved_total": recorded_total - total,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "has_more": normalized_offset + len(rows) < total,
        }
        if activity is not None:
            payload["_resolved_activity"] = signatures
        return payload

    def run_snapshot_activity(
        self,
        run_id: int,
        *,
        after_snapshot_id: int = 0,
        limit: int = 2_000,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT s.id, s.observed_at, c.hostname, s.final_score,
                   CASE
                       WHEN julianday(s.observed_at)>=julianday(r.started_at) THEN 1
                       ELSE 0
                   END AS observation_current_run,
                   COALESCE(json_extract(s.observation_json, '$.error'), '') AS error,
                   COALESCE(
                       json_extract(s.observation_json, '$.fetch_outcome'), 'unknown'
                   ) AS fetch_outcome,
                   COALESCE(
                       json_extract(s.observation_json, '$.fetch_disposition'), ''
                   ) AS fetch_disposition
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            JOIN runs r ON r.id=s.run_id
            WHERE s.run_id=? AND s.id>?
            ORDER BY s.id ASC LIMIT ?
            """,
            (int(run_id), max(0, int(after_snapshot_id)), max(1, min(int(limit), 10_000))),
        ).fetchall()
        return [
            {
                **dict(row),
                "observation_current_run": bool(row["observation_current_run"]),
            }
            for row in rows
        ]

    def platform_identity_recovery_candidates(
        self,
        *,
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT c.id AS candidate_id, c.hostname, c.source_apex,
                   c.label_hint, c.discovery_rank, s.observed_at,
                   COALESCE(
                       json_extract(s.observation_json, '$.tenant_label'), ''
                   ) AS tenant_label,
                   COALESCE(
                       json_extract(s.observation_json, '$.fetch_outcome'), 'unknown'
                   ) AS fetch_outcome
            FROM snapshots s
            JOIN candidates c ON c.id=s.candidate_id
            WHERE CASE WHEN json_valid(observation_json)
                AND (ai_json IS NULL OR json_valid(ai_json)) THEN
                COALESCE(json_extract(observation_json, '$.canonical_recovery_required'), 0)=1
                AND COALESCE(json_extract(ai_json, '$.company_url'), '')=''
                ELSE 0 END
              AND NOT EXISTS (
                  SELECT 1 FROM snapshots newer
                  WHERE newer.candidate_id=s.candidate_id AND newer.id>s.id
              )
            ORDER BY c.discovery_rank DESC, s.observed_at DESC, c.id DESC
            LIMIT ?
            """,
            (-1 if limit is None else max(1, min(int(limit), 10_000)),),
        ).fetchall()
        return [
            dict(row)
            for row in rows
            if is_recoverable_tenant_label(str(row["tenant_label"] or row["label_hint"] or ""))
        ]

    def run_source_error_activity(
        self,
        run_id: int,
        *,
        after_error_id: int = 0,
        limit: int = 2_000,
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, source_apex, error, created_at
            FROM source_errors
            WHERE run_id=? AND id>?
            ORDER BY id ASC LIMIT ?
            """,
            (int(run_id), max(0, int(after_error_id)), max(1, min(int(limit), 10_000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _smart_scan_candidate_rows(
        self,
        *,
        after_candidate_id: int = 0,
        max_candidate_id: int | None = None,
        limit: int = 2_000,
        include_core_reliability: bool = False,
        include_discovery: bool = True,
    ) -> list[dict[str, Any]]:
        boundary = (
            int(max_candidate_id)
            if max_candidate_id is not None
            else int(
                self.connection.execute("SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0]
            )
        )
        discovery_fields = (
            "c.first_seen,c.discovery_rank,c.label_hint,c.discovery_evidence_json,"
            "c.provider_record_ids_json,c.discovery_assertions_json,"
            if include_discovery else ""
        )
        discovery_join = "JOIN candidates c ON c.id=page.id" if include_discovery else ""
        canonical_domain = "ti.canonical_domain"
        canonical_join = (
            "LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=page.snapshot_id"
        )
        if not include_discovery and self.connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name IN "
            "('engine_mission_members','idx_snapshot_target_canonical') "
            "AND type IN ('table','index')"
        ).fetchone()[0] == 2:
            # A membership hint can find the exact row in the narrow covering index.
            # Missing or older hints still resolve by the authoritative snapshot ID.
            canonical_join = "LEFT JOIN engine_mission_members m ON m.candidate_id=page.id"
            canonical_domain = """COALESCE((
                SELECT ti.canonical_domain FROM snapshot_target_intelligence ti
                    INDEXED BY idx_snapshot_target_canonical
                WHERE ti.canonical_domain=m.company_key AND ti.snapshot_id=page.snapshot_id
            ),(
                SELECT ti.canonical_domain FROM snapshot_target_intelligence ti
                WHERE ti.snapshot_id=page.snapshot_id
            ))"""
        # The covering candidate index supplies the latest ID without opening the snapshot
        # payload. A materialized ID page computes it once even if its projection is absent.
        rows = self.connection.execute(
            f"""
            WITH page AS MATERIALIZED (
                SELECT c.id,c.source_apex,c.hostname,
                       (SELECT MAX(s.id) FROM snapshots s WHERE s.candidate_id=c.id)
                           AS snapshot_id
                FROM candidates c
                WHERE c.id>? AND c.id<=?
                ORDER BY c.id LIMIT ?
            )
            SELECT page.id,page.source_apex,page.hostname,{discovery_fields}
                   page.snapshot_id,{canonical_domain} AS canonical_domain,
                   COALESCE(w.scan_enabled, 1) AS scan_enabled
            FROM page {discovery_join}
            {canonical_join}
            LEFT JOIN prospect_workflow w ON w.candidate_id=page.id
            ORDER BY page.id
            """,
            (
                max(0, int(after_candidate_id)),
                max(0, boundary),
                max(1, int(limit)),
            ),
        ).fetchall()
        keys = [str(row["canonical_domain"] or row["hostname"]) for row in rows]
        reliability = self._smart_scan_core_reliability(keys) if include_core_reliability else {}
        return [dict(row, scan_attempt_count=reliability.get(key, (0, 0))[0],
                     scan_failure_count=reliability.get(key, (0, 0))[1])
                for row, key in zip(rows, keys, strict=True)]

    def _smart_scan_core_reliability(self, company_keys: list[str]):
        """Website retrieval receipts only; duplicate checkpoints/reuse are one attempt."""
        now = datetime.now(UTC)
        result = {}
        keys = list(dict.fromkeys(company_keys))
        for start in range(0, len(keys), 400):
            batch = keys[start:start + 400]
            for row in self.connection.execute(
                f"""WITH receipts AS MATERIALIZED (
                    SELECT ti.canonical_domain AS company_key, s.observed_at,
                           json_extract(s.observation_json,
                               '$.live','$.fetch_outcome','$.error') AS receipt
                    FROM snapshot_target_intelligence ti JOIN snapshots s ON s.id=ti.snapshot_id
                    WHERE ti.canonical_domain IN ({','.join('?' for _ in batch)})
                        AND julianday(s.observed_at)>=julianday(?)
                        AND julianday(s.observed_at)<=julianday(?)
                )
                SELECT company_key,COUNT(*) AS attempts,SUM(failed) AS failures FROM (
                    SELECT company_key,observed_at,
                        MAX(CASE WHEN json_extract(receipt,'$[0]')=0 AND (
                            json_extract(receipt,'$[1]') IN (
                                'retryable_error','platform_blocked') OR
                            length(COALESCE(json_extract(receipt,'$[2]'),''))>0
                        ) THEN 1 ELSE 0 END) AS failed
                    FROM receipts
                    WHERE COALESCE(json_extract(receipt,'$[1]'),'')<>'circuit_open'
                      AND COALESCE(json_extract(receipt,'$[2]'),'') NOT LIKE 'PoolTimeout:%'
                    GROUP BY company_key,observed_at
                ) GROUP BY company_key""",
                (*batch, (now - timedelta(days=SCAN_RELIABILITY_LOOKBACK_DAYS)).isoformat(),
                 now.isoformat()),
            ):
                result[row["company_key"]] = (int(row["attempts"]), int(row["failures"]))
        return result

    @staticmethod
    def _smart_scan_reliability_skip_reason(
        row: sqlite3.Row,
        max_company_failure_rate: float | None,
    ) -> str:
        if not bool(row["scan_enabled"]):
            return "manual"
        attempts = int(row["scan_attempt_count"] or 0)
        failures = int(row["scan_failure_count"] or 0)
        if (
            max_company_failure_rate is not None
            and attempts >= SCAN_RELIABILITY_MIN_ATTEMPTS
            and failures / attempts * 100 >= float(max_company_failure_rate)
        ):
            return "failure_rate"
        return ""

    def _smart_scan_method_reliability(
        self, company_keys: list[str],
    ) -> dict[tuple[str, str], tuple[int, int]]:
        """Recent terminal attempts by canonical company and method, across versions."""
        result: dict[tuple[str, str], tuple[int, int]] = {}
        cutoff = (datetime.now(UTC) - timedelta(days=SCAN_RELIABILITY_LOOKBACK_DAYS)).isoformat()
        attempts = sorted(SCAN_RELIABILITY_ATTEMPT_STATUSES)
        failures = sorted(SCAN_RELIABILITY_FAILURE_STATUSES)
        keys = list(dict.fromkeys(key for key in company_keys if key))
        for start in range(0, len(keys), 400):
            batch = keys[start : start + 400]
            rows = self.connection.execute(
                f"""
                SELECT company_key, scanner_id, COUNT(*) AS attempts,
                       SUM(CASE WHEN status IN ({','.join('?' for _ in failures)})
                           THEN 1 ELSE 0 END) AS failures
                FROM scanner_executions
                WHERE company_key IN ({','.join('?' for _ in batch)})
                  AND status IN ({','.join('?' for _ in attempts)})
                  AND julianday(started_at)>=julianday(?)
                GROUP BY company_key, scanner_id
                """,
                [*failures, *batch, *attempts, cutoff],
            ).fetchall()
            for row in rows:
                result[(str(row["company_key"]), str(row["scanner_id"]))] = (
                    int(row["attempts"]), int(row["failures"]),
                )
        return result

    def _smart_scan_company_work(
        self,
        company_keys: list[str],
    ) -> dict[str, sqlite3.Row]:
        work: dict[str, sqlite3.Row] = {}
        unique_keys = list(dict.fromkeys(key for key in company_keys if key))
        for start in range(0, len(unique_keys), 500):
            batch = unique_keys[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT company_key, status, completed_at, lease_expires_at, next_retry_at
                FROM company_scan_work
                WHERE effort_key=? AND company_key IN ({placeholders})
                """,
                [COMPANY_SCAN_EFFORT, *batch],
            ).fetchall()
            work.update({str(row["company_key"]): row for row in rows})
        return work

    @staticmethod
    def _smart_scan_work_state(
        work: sqlite3.Row | None,
        *,
        now: datetime,
    ) -> str:
        if work is None:
            return "due"
        if work["status"] == "running" and LeadStore._lease_is_active(
            work["lease_expires_at"],
            now=now,
        ):
            return "active"
        if work["status"] == "failed":
            next_retry_at = LeadStore._datetime_or_none(work["next_retry_at"])
            if next_retry_at is not None and next_retry_at > now:
                return "retry_deferred"
        if work["status"] != "complete" or not work["completed_at"]:
            return "due"
        try:
            completed_at = datetime.fromisoformat(str(work["completed_at"]))
        except (TypeError, ValueError):
            return "due"
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        return (
            "fresh"
            if completed_at > now - timedelta(hours=COMPANY_SCAN_FRESH_HOURS)
            else "due"
        )

    def smart_scan_inventory_valid_until(
        self, now: datetime, *, include_failure_history: bool,
    ) -> datetime:
        """Earliest possible change to census counts without a database write.

        Reading all company clocks is conservative: even an irrelevant company's
        transition may invalidate reuse, but no eligible transition can be missed.
        """
        deadline = datetime.max.replace(tzinfo=UTC)
        for row in self.connection.execute(
            "SELECT status,completed_at,lease_expires_at,next_retry_at "
            "FROM company_scan_work WHERE effort_key=?", (COMPANY_SCAN_EFFORT,),
        ):
            field = {"running": "lease_expires_at", "failed": "next_retry_at",
                     "complete": "completed_at"}.get(row["status"])
            stamp = self._datetime_or_none(row[field]) if field else None
            if stamp is not None:
                if row["status"] == "complete":
                    try:
                        stamp += timedelta(hours=COMPANY_SCAN_FRESH_HOURS)
                    except OverflowError:
                        return now  # Retain authoritative counts without caching malformed clocks.
                if now < stamp < deadline:
                    deadline = stamp
        if include_failure_history:
            # Match SQLite's timestamp interpretation used by the receipt query.
            # Include future receipts entering the window, as well as current ones
            # leaving it. Circuit-open and suppressed receipts may invalidate early.
            stamp = now.isoformat()
            row = self.connection.execute(
                "SELECT MIN(CASE WHEN julianday(s.observed_at)>julianday(?) "
                "THEN julianday(s.observed_at) ELSE julianday(s.observed_at)+? END) "
                "FROM snapshots s JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
                "WHERE julianday(s.observed_at)>=julianday(?)",
                (stamp, SCAN_RELIABILITY_LOOKBACK_DAYS,
                 (now - timedelta(days=SCAN_RELIABILITY_LOOKBACK_DAYS)).isoformat()),
            ).fetchone()
            if row[0] is not None:
                # julianday rounds to milliseconds. Expire slightly early, never late.
                deadline = min(deadline, datetime.fromtimestamp(
                    (row[0] - 2440587.5) * 86400, UTC,
                ) - timedelta(milliseconds=1))
        return deadline

    def smart_scan_inventory(
        self,
        *,
        max_candidate_id: int | None = None,
        after_candidate_id: int = 0,
        max_company_failure_rate: float | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, int]:
        """Count the full candidate base against the hard company-hydration clock.

        This is an advisory, read-only plan. Child scans still perform the authoritative
        canonical-company freshness check and atomic claim before network work.
        """

        latest_id = int(
            self.connection.execute("SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0]
        )
        boundary = (
            latest_id
            if max_candidate_id is None
            else min(max(0, int(max_candidate_id)), latest_id)
        )
        totals = {
            "maximum_candidate_id": boundary,
            "total_candidates": 0,
            "eligible_candidates": 0,
            "suppressed_candidates": 0,
            "manually_disabled_candidates": 0,
            "failure_rate_skipped_candidates": 0,
            "fresh_candidates": 0,
            "due_candidates": 0,
            "active_candidates": 0,
            "retry_deferred_candidates": 0,
            "uninspected_candidates": 0,
            "scanner_candidates": 0,
        }
        cursor = min(boundary, max(0, int(after_candidate_id)))
        now = datetime.now(UTC)
        while cursor < boundary:
            if progress:
                progress(f"Checking company inventory · after ID {cursor:,} of {boundary:,}")
            rows = self._smart_scan_candidate_rows(
                after_candidate_id=cursor,
                max_candidate_id=boundary,
                include_core_reliability=max_company_failure_rate is not None,
                include_discovery=False,
            )
            if not rows:
                break
            cursor = int(rows[-1]["id"])
            if progress:
                progress(f"Checking company inventory · through ID {cursor:,} of {boundary:,}")
            keys = [
                self._normalize_identity_host(row["canonical_domain"])
                or self._normalize_identity_host(row["hostname"])
                for row in rows
            ]
            work_by_key = self._smart_scan_company_work(keys)
            for row, company_key in zip(rows, keys, strict=True):
                totals["total_candidates"] += 1
                if row["snapshot_id"] is None:
                    totals["uninspected_candidates"] += 1
                if _target_is_suppressed(
                    str(row["source_apex"]),
                    str(row["hostname"]),
                    row["canonical_domain"],
                ):
                    totals["suppressed_candidates"] += 1
                    continue
                skip_reason = self._smart_scan_reliability_skip_reason(
                    row, max_company_failure_rate
                )
                if skip_reason == "manual":
                    totals["manually_disabled_candidates"] += 1
                    continue
                if skip_reason == "failure_rate":
                    totals["failure_rate_skipped_candidates"] += 1
                    continue
                totals["eligible_candidates"] += 1
                if row["snapshot_id"] is not None:
                    totals["scanner_candidates"] += 1
                state = self._smart_scan_work_state(work_by_key.get(company_key), now=now)
                totals[f"{state}_candidates"] += 1
        return totals

    def smart_scan_due_candidates(
        self,
        *,
        after_candidate_id: int = 0,
        max_candidate_id: int | None = None,
        limit: int = 1_000,
        max_company_failure_rate: float | None = None,
    ) -> dict[str, Any]:
        """Return one stable-id page of monthly-due, non-suppressed candidate work."""

        latest_id = int(
            self.connection.execute("SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0]
        )
        boundary = (
            latest_id
            if max_candidate_id is None
            else min(max(0, int(max_candidate_id)), latest_id)
        )
        cursor = max(0, int(after_candidate_id))
        maximum = max(1, min(int(limit), 10_000))
        candidates: list[DiscoveredHost] = []
        now = datetime.now(UTC)
        while cursor < boundary and len(candidates) < maximum:
            rows = self._smart_scan_candidate_rows(
                after_candidate_id=cursor,
                max_candidate_id=boundary,
                limit=max(2_000, maximum * 2),
                include_core_reliability=max_company_failure_rate is not None,
            )
            if not rows:
                break
            keys = [
                self._normalize_identity_host(row["canonical_domain"])
                or self._normalize_identity_host(row["hostname"])
                for row in rows
            ]
            work_by_key = self._smart_scan_company_work(keys)
            for row, company_key in zip(rows, keys, strict=True):
                cursor = int(row["id"])
                if _target_is_suppressed(
                    str(row["source_apex"]),
                    str(row["hostname"]),
                    row["canonical_domain"],
                ):
                    continue
                if self._smart_scan_reliability_skip_reason(
                    row, max_company_failure_rate
                ):
                    continue
                if self._smart_scan_work_state(work_by_key.get(company_key), now=now) != "due":
                    continue
                candidates.append(
                    DiscoveredHost(
                        source_apex=str(row["source_apex"]),
                        hostname=str(row["hostname"]),
                        first_seen=(
                            datetime.fromisoformat(str(row["first_seen"]))
                            if row["first_seen"]
                            else None
                        ),
                        discovery_rank=float(row["discovery_rank"] or 0),
                        label_hint=str(row["label_hint"] or ""),
                        discovery_evidence_urls=json.loads(
                            row["discovery_evidence_json"] or "[]"
                        ),
                        provider_record_ids=json.loads(
                            row["provider_record_ids_json"] or "[]"
                        ),
                        discovery_assertions=json.loads(
                            row["discovery_assertions_json"] or "[]"
                        ),
                    )
                )
                if len(candidates) >= maximum:
                    break
        return {
            "items": candidates,
            "next_candidate_id": cursor,
            "maximum_candidate_id": boundary,
            "exhausted": cursor >= boundary,
        }

    def smart_scan_scanner_gap_candidates(
        self,
        scanner_versions: list[tuple[str, str]],
        *,
        after_candidate_id: int = 0,
        max_candidate_id: int | None = None,
        limit: int = 25,
        max_company_failure_rate: float | None = None,
    ) -> dict[str, Any]:
        """Page inspected companies with at least one due scanner-specific gap.

        A recent reusable execution, active lease, future retry clock, or recent explicit
        non-applicability decision closes that scanner gap for this pass. The scanner runtime
        remains authoritative when the batch executes.
        """
        from .company_research import PUBLIC_NETWORK_SCANNERS

        normalized_scanners = list(
            dict.fromkeys(
                (str(scanner_id).strip(), str(version).strip())
                for scanner_id, version in scanner_versions
                if str(scanner_id).strip() and str(version).strip()
            )
        )
        latest_id = int(
            self.connection.execute("SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0]
        )
        boundary = (
            latest_id
            if max_candidate_id is None
            else min(max(0, int(max_candidate_id)), latest_id)
        )
        cursor = max(0, int(after_candidate_id))
        maximum = max(1, min(int(limit), 500))
        if not normalized_scanners:
            return {
                "items": [],
                "scanner_ids_by_hostname": {},
                "next_candidate_id": boundary,
                "maximum_candidate_id": boundary,
                "exhausted": True,
            }
        candidates: list[DiscoveredHost] = []
        scanner_ids_by_hostname: dict[str, list[str]] = {}
        now = datetime.now(UTC)
        freshness_cutoff = now - timedelta(hours=SCANNER_WORK_FRESH_HOURS)
        seo_top_company_keys = (
            self.dataforseo_ranked_company_keys(limit=2_500)
            if any(scanner_id == SEO_READINESS_SCANNER_ID for scanner_id, _ in normalized_scanners)
            else set()
        )
        while cursor < boundary and len(candidates) < maximum:
            rows = self._smart_scan_candidate_rows(
                after_candidate_id=cursor,
                max_candidate_id=boundary,
                # Match the output page so dense gaps do not repeatedly load overlapping
                # 1,000-company scanner ledgers before each 25-company child run.
                limit=maximum,
            )
            if not rows:
                break
            keys = [
                self._normalize_identity_host(row["canonical_domain"])
                or self._normalize_identity_host(row["hostname"])
                for row in rows
            ]
            company_work = self._smart_scan_company_work(keys)
            unique_keys = list(dict.fromkeys(key for key in keys if key))
            reliability_by_method = (
                self._smart_scan_method_reliability(unique_keys)
                if max_company_failure_rate is not None else {}
            )
            work_by_method: dict[tuple[str, str], list[sqlite3.Row]] = {}
            recent_inapplicable: set[tuple[str, str]] = set()
            research_holds: set[str] = set()
            for start in range(0, len(unique_keys), 400):
                batch = unique_keys[start : start + 400]
                placeholders = ", ".join("?" for _ in batch)
                research_holds.update(str(item[0]) for item in self.connection.execute(
                    "SELECT company_key FROM company_research_backoff "
                    f"WHERE lane='public' AND company_key IN ({placeholders}) AND hold_until>?",
                    (*batch, now.isoformat()),
                ))
                work_rows = self.connection.execute(
                    f"""
                    SELECT company_key, scanner_id, scanner_version, status,
                           completed_at, next_due_at, lease_expires_at
                    FROM scanner_work
                    WHERE company_key IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                for item in work_rows:
                    method = (str(item["company_key"]), str(item["scanner_id"]))
                    work_by_method.setdefault(method, []).append(item)
                decision_rows = self.connection.execute(
                    f"""
                    SELECT decision.company_key, decision.scanner_id,
                           decision.scanner_version, decision.decided_at
                    FROM scanner_schedule_decisions decision
                    JOIN (
                        SELECT company_key, scanner_id, MAX(id) AS latest_id
                        FROM scanner_schedule_decisions
                        WHERE company_key IN ({placeholders})
                        GROUP BY company_key, scanner_id
                    ) latest ON latest.latest_id=decision.id
                    WHERE decision.applicable=0
                    """,
                    batch,
                ).fetchall()
                for item in decision_rows:
                    decided_at = self._datetime_or_none(item["decided_at"])
                    if decided_at is not None and decided_at > freshness_cutoff:
                        recent_inapplicable.add(
                            (
                                str(item["company_key"]),
                                str(item["scanner_id"]),
                            )
                        )
            for row, company_key in zip(rows, keys, strict=True):
                cursor = int(row["id"])
                if row["snapshot_id"] is None or _target_is_suppressed(
                    str(row["source_apex"]),
                    str(row["hostname"]),
                    row["canonical_domain"],
                ):
                    continue
                # A failing sibling method cannot suppress healthy or unattempted work.
                if not bool(row["scan_enabled"]):
                    continue
                # This pass deliberately opens no company pages. Only hand it snapshots the
                # shared company ledger guarantees the child scan can reuse inside 90 days.
                if self._smart_scan_work_state(company_work.get(company_key), now=now) != "fresh":
                    continue
                due_scanner_ids: list[str] = []
                for scanner_id, _scanner_version in normalized_scanners:
                    if company_key in research_holds and scanner_id in PUBLIC_NETWORK_SCANNERS:
                        continue
                    if (
                        scanner_id == SEO_READINESS_SCANNER_ID
                        and company_key not in seo_top_company_keys
                    ):
                        continue
                    method = (company_key, scanner_id)
                    attempts, failures = reliability_by_method.get(method, (0, 0))
                    if (
                        max_company_failure_rate is not None
                        and attempts >= SCAN_RELIABILITY_MIN_ATTEMPTS
                        and failures / attempts * 100 >= max_company_failure_rate
                    ):
                        continue
                    method_work = work_by_method.get(method, [])
                    if method in recent_inapplicable:
                        continue
                    if not method_work:
                        due_scanner_ids.append(scanner_id)
                        continue
                    if any(
                        item["status"] == "running"
                        and self._lease_is_active(item["lease_expires_at"], now=now)
                        for item in method_work
                    ):
                        continue
                    method_freshness_cutoff = now - (
                        timedelta(days=SEO_REFRESH_DAYS)
                        if scanner_id == SEO_READINESS_SCANNER_ID
                        else timedelta(hours=SCANNER_WORK_FRESH_HOURS)
                    )
                    if any(
                        item["status"] in SCANNER_REUSABLE_STATUSES
                        and (
                            completed_at := self._datetime_or_none(item["completed_at"])
                        ) is not None
                        and completed_at > method_freshness_cutoff
                        for item in method_work
                    ):
                        continue
                    if any(
                        item["status"] in SCANNER_TERMINAL_STATUSES
                        and (
                            next_due_at := self._datetime_or_none(item["next_due_at"])
                        ) is not None
                        and next_due_at > now
                        for item in method_work
                    ):
                        continue
                    due_scanner_ids.append(scanner_id)
                if not due_scanner_ids:
                    continue
                candidate = DiscoveredHost(
                    source_apex=str(row["source_apex"]),
                    hostname=str(row["hostname"]),
                    first_seen=(
                        datetime.fromisoformat(str(row["first_seen"]))
                        if row["first_seen"]
                        else None
                    ),
                    discovery_rank=float(row["discovery_rank"] or 0),
                    label_hint=str(row["label_hint"] or ""),
                    discovery_evidence_urls=json.loads(
                        row["discovery_evidence_json"] or "[]"
                    ),
                    provider_record_ids=json.loads(
                        row["provider_record_ids_json"] or "[]"
                    ),
                    discovery_assertions=json.loads(
                        row["discovery_assertions_json"] or "[]"
                    ),
                )
                candidates.append(candidate)
                scanner_ids_by_hostname[candidate.hostname] = due_scanner_ids
                if len(candidates) >= maximum:
                    break
        return {
            "items": candidates,
            "scanner_ids_by_hostname": scanner_ids_by_hostname,
            "next_candidate_id": cursor,
            "maximum_candidate_id": boundary,
            "exhausted": cursor >= boundary,
        }

    def similar_prospects(self, candidate_id: int, *, limit: int = 12) -> list[dict[str, Any]]:
        """Rank already-inspected peers with transparent, deterministic overlap reasons."""
        seed = self.prospect_detail(
            candidate_id,
            include_ad_intelligence=False,
            include_current_frontier=False,
            include_people_intelligence=False,
        )
        if seed is None:
            return []
        seed_score = seed["score"]
        seed_placement = seed_score.get("placement", {})
        seed_specialties = set(seed_placement.get("agency_specialties", []))
        # Signals live on the observation payload; `_prospect_dict` intentionally exposes the
        # summarized web/score contract, so use the stored detail record for the seed and peers.
        seed_detail = self.connection.execute(
            """
            SELECT observation_json FROM snapshots
            WHERE candidate_id=? ORDER BY id DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        seed_observation = (
            SiteObservation.model_validate_json(seed_detail["observation_json"])
            if seed_detail
            else SiteObservation(hostname=seed["hostname"])
        )
        seed_signals = {
            value
            for key in ("agency", "photo_video_services", "creative_demand", "placement_demand")
            for value in seed_observation.signals.get(key, [])
        }
        seed_technologies = set(seed_observation.technologies)
        candidates = self.list_prospects(
            live=True,
            sort="placement",
            limit=max(500, limit * 30),
        )["items"]
        peer_signals_by_id = self._latest_observation_signals_bulk(
            [peer["candidate_id"] for peer in candidates]
        )
        ranked: list[tuple[float, dict[str, Any]]] = []
        seed_identity = (seed.get("canonical_host") or seed["hostname"]).removeprefix("www.")
        for peer in candidates:
            if peer["candidate_id"] == candidate_id or peer.get("disqualified"):
                continue
            identity = (peer.get("canonical_host") or peer["hostname"]).removeprefix("www.")
            if identity == seed_identity:
                continue
            peer_placement = peer["score"].get("placement", {})
            peer_specialties = set(peer_placement.get("agency_specialties", []))
            peer_signals = {
                value
                for key in (
                    "agency",
                    "photo_video_services",
                    "creative_demand",
                    "placement_demand",
                )
                for value in peer_signals_by_id.get(peer["candidate_id"], {}).get(key, [])
            }
            peer_technologies = set(peer.get("technologies", []))
            reasons: list[str] = []
            score_value = 0.0
            if peer["archetype"] == seed["archetype"]:
                score_value += 28
                reasons.append(f"Same {peer['archetype'].replace('_', ' ')} type")
            shared_specialties = sorted(seed_specialties & peer_specialties)
            if shared_specialties:
                score_value += min(28, 10 + 6 * len(shared_specialties))
                reasons.append("Shared specialty: " + ", ".join(shared_specialties[:3]))
            shared_signals = sorted(seed_signals & peer_signals)
            if shared_signals:
                score_value += min(22, 6 + 4 * len(shared_signals))
                reasons.append("Shared demand: " + ", ".join(shared_signals[:3]))
            shared_technologies = sorted(seed_technologies & peer_technologies)
            if shared_technologies:
                score_value += min(12, 3 * len(shared_technologies))
                reasons.append("Shared stack: " + ", ".join(shared_technologies[:3]))
            placement_gap = abs(
                int(seed_placement.get("score", 0)) - int(peer_placement.get("score", 0))
            )
            score_value += max(0, 10 - placement_gap / 5)
            if peer["source_apex"] == seed["source_apex"]:
                score_value += 4
            if score_value < 18:
                continue
            result = dict(peer)
            result["similarity_score"] = min(100, round(score_value))
            result["similarity_reasons"] = reasons[:4]
            ranked.append((score_value, result))
        ranked.sort(
            key=lambda item: (
                item[0],
                item[1]["score"].get("placement", {}).get("score", 0),
                item[1]["final_score"],
            ),
            reverse=True,
        )
        return [item for _, item in ranked[: max(1, min(limit, 30))]]

    def _latest_observation_signals(self, candidate_id: int) -> dict[str, list[str]]:
        row = self.connection.execute(
            "SELECT observation_json FROM snapshots WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return {}
        return SiteObservation.model_validate_json(row["observation_json"]).signals

    def _latest_observation_signals_bulk(
        self, candidate_ids: list[int]
    ) -> dict[int, dict[str, list[str]]]:
        result: dict[int, dict[str, list[str]]] = {}
        unique_ids = list(dict.fromkeys(candidate_ids))
        for start in range(0, len(unique_ids), 500):
            batch = unique_ids[start : start + 500]
            if not batch:
                continue
            placeholders = ", ".join("?" for _ in batch)
            rows = self.connection.execute(
                f"""
                SELECT s.candidate_id, s.observation_json
                FROM snapshots s
                JOIN (
                    SELECT candidate_id, MAX(id) AS snapshot_id
                    FROM snapshots
                    WHERE candidate_id IN ({placeholders})
                    GROUP BY candidate_id
                ) latest ON latest.snapshot_id=s.id
                """,
                batch,
            ).fetchall()
            for row in rows:
                observation = SiteObservation.model_validate_json(row["observation_json"])
                result[int(row["candidate_id"])] = observation.signals
        return result

    def prospect_detail(
        self,
        candidate_id: int,
        *,
        include_ad_intelligence: bool = True,
        include_current_frontier: bool = True,
        include_people_intelligence: bool = True,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            f"""
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots
                WHERE candidate_id=?
                GROUP BY candidate_id
            )
            SELECT c.id AS candidate_id, c.hostname, c.source_apex, c.first_seen,
                   c.label_hint, c.discovery_evidence_json, c.provider_record_ids_json,
                   s.run_id, s.observed_at, s.observation_json, s.score_json,
                   s.ai_json, s.final_score,
                   {TARGET_PROJECTION_SELECT},
                   COALESCE(w.stage, 'research') AS stage,
                   COALESCE(w.starred, 0) AS starred,
                   COALESCE(w.excluded, 0) AS excluded,
                   COALESCE(w.scan_enabled, 1) AS scan_enabled,
                   COALESCE(w.notes, '') AS notes,
                   w.next_follow_up_at,
                   COALESCE((
                       SELECT json_group_array(tag) FROM (
                           SELECT tag FROM prospect_tags
                           WHERE candidate_id=c.id ORDER BY tag
                       )
                   ), '[]') AS tags_json,
                   COALESCE((
                       SELECT COUNT(*) FROM prospect_activities pa
                       WHERE pa.candidate_id=c.id
                         AND pa.activity_type IN (
                             'email_sent', 'linkedin_message', 'call', 'outreach_recorded'
                         )
                   ), 0) AS contact_count,
                   (
                       SELECT MAX(pa.occurred_at) FROM prospect_activities pa
                       WHERE pa.candidate_id=c.id
                         AND pa.activity_type IN (
                             'email_sent', 'linkedin_message', 'call', 'outreach_recorded'
                         )
                   ) AS last_contacted_at,
                   EXISTS(
                       SELECT 1 FROM changes ch
                       WHERE ch.candidate_id=c.id AND ch.run_id=s.run_id
                         AND ch.kind<>'new_candidate'
                   ) AS changed
            FROM latest l
            JOIN snapshots s ON s.id=l.snapshot_id
            JOIN candidates c ON c.id=l.candidate_id
            LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE c.id=?
            """,
            (candidate_id, candidate_id),
        ).fetchone()
        if row is None:
            return None
        prospect = self._prospect_dict(row)
        prospect.update(self.candidate_scan_reliability(candidate_id))
        company_key = str(
            (prospect.get("target_intelligence") or {}).get("canonical_domain")
            or prospect.get("canonical_host")
            or prospect.get("hostname")
            or ""
        )
        if include_people_intelligence:
            prospect["people_intelligence"] = self.treg_people_intelligence(company_key)
        if company_key and include_current_frontier:
            prospect["web"]["frontier"] = self.frontier_intelligence(company_key).model_dump(
                mode="json"
            )
        rows = self.connection.execute(
            f"""
            SELECT s.id, s.run_id, s.observed_at, s.observation_json, s.score_json,
                   s.ai_json, s.final_score, {TARGET_PROJECTION_SELECT}
            FROM snapshots s
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE s.candidate_id=? ORDER BY s.id DESC LIMIT 24
            """,
            (candidate_id,),
        ).fetchall()
        change_rows = self.connection.execute(
            "SELECT run_id, details_json FROM changes WHERE candidate_id=? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        changes_by_run: dict[int, list[dict[str, Any]]] = {}
        for change in change_rows:
            changes_by_run.setdefault(int(change["run_id"]), []).append(
                json.loads(change["details_json"])
            )
        history = []
        for row in rows:
            observation = SiteObservation.model_validate_json(row["observation_json"])
            score = ScoreCard.model_validate_json(row["score_json"])
            target_intelligence = self._target_intelligence_from_row(row)
            if target_intelligence is not None:
                score.creative_capacity = CreativeCapacityHypothesis.model_validate(
                    target_intelligence["hypothesis"]
                )
            history.append(
                {
                    "snapshot_id": row["id"],
                    "run_id": row["run_id"],
                    "observed_at": row["observed_at"],
                    "live": observation.live,
                    "status_code": observation.status_code,
                    "final_url": observation.final_url,
                    "title": observation.title,
                    "final_score": row["final_score"],
                    "base_score": score.total,
                    "contact_priority": score.contact_priority,
                    "demand_score": score.demand_score,
                    "capacity_score": score.capacity_score,
                    "timing_score": score.timing_score,
                    "growth_score": score.growth_score,
                    "placement_score": score.placement.score,
                    "placement_confidence": score.placement.confidence,
                    "placement_ready": score.placement.ready,
                    "target_intelligence": target_intelligence,
                    "changes": changes_by_run.get(int(row["run_id"]), []),
                }
            )
        prospect["history"] = history
        activity_rows = self.connection.execute(
            """
            SELECT id, activity_type, summary, contact_email, occurred_at, created_at
            FROM prospect_activities
            WHERE candidate_id=? ORDER BY occurred_at DESC, id DESC LIMIT 100
            """,
            (candidate_id,),
        ).fetchall()
        prospect["activities"] = [dict(row) for row in activity_rows]
        if include_ad_intelligence:
            prospect["ad_intelligence"] = self.current_candidate_ad_intelligence(candidate_id)
            prospect["advertiser_domain_links"] = self.list_advertiser_domain_links(
                candidate_id=candidate_id,
                limit=100,
            )["items"]
        return prospect

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "config": json.loads(row["config_json"]),
                "summary": json.loads(row["summary_json"]) if row["summary_json"] else {},
            }
            for row in rows
        ]

    def dashboard_payload(self) -> dict[str, Any]:
        latest_run = self.run_metadata()
        latest_run_id = latest_run.get("id")
        prospect_result = self.list_prospects(limit=8)
        metrics = self.connection.execute(
            """
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            )
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.observation_json, '$.live') THEN 1 ELSE 0 END
                   ), 0) AS live,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.score_json, '$.budget_ready') THEN 1 ELSE 0 END
                   ), 0) AS qualified,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.score_json, '$.placement.ready') THEN 1 ELSE 0 END
                   ), 0) AS placement_ready,
                   COALESCE(SUM(
                       CASE WHEN EXISTS(
                           SELECT 1 FROM json_each(
                               json_extract(s.score_json, '$.placement.specialist_roles')
                           ) WHERE value='ai_photo'
                       ) THEN 1 ELSE 0 END
                   ), 0) AS photo_fit,
                   COALESCE(SUM(
                       CASE WHEN EXISTS(
                           SELECT 1 FROM json_each(
                               json_extract(s.score_json, '$.placement.specialist_roles')
                           ) WHERE value='ai_video'
                       ) THEN 1 ELSE 0 END
                   ), 0) AS video_fit,
                   COALESCE(SUM(
                       CASE WHEN json_extract(s.score_json, '$.archetype')='agency'
                                  AND COALESCE(
                                      json_extract(s.score_json, '$.placement.score'), 0
                                  ) >= 60
                       THEN 1 ELSE 0 END
                   ), 0) AS agency_ready,
                   COALESCE(SUM(
                       CASE WHEN s.final_score >= 80 THEN 1 ELSE 0 END
                   ), 0) AS preview_ready,
                   COALESCE(SUM(CASE WHEN COALESCE(w.starred, 0)=1 THEN 1 ELSE 0 END), 0) AS starred
            FROM latest l
            JOIN snapshots s ON s.id=l.snapshot_id
            LEFT JOIN prospect_workflow w ON w.candidate_id=l.candidate_id
            """
        ).fetchone()
        pipeline_rows = self.connection.execute(
            """
            SELECT COALESCE(w.stage, 'research') AS stage, COUNT(*) AS count
            FROM candidates c
            LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
            GROUP BY COALESCE(w.stage, 'research')
            """
        ).fetchall()
        changes = []
        if latest_run_id:
            change_rows = self.connection.execute(
                """
                SELECT ch.kind, ch.details_json, ch.created_at, c.id AS candidate_id,
                       c.hostname, c.label_hint, s.final_score,
                       json_extract(s.observation_json, '$.title') AS title,
                       json_extract(s.observation_json, '$.canonical_host') AS canonical_host
                FROM changes ch
                JOIN candidates c ON c.id=ch.candidate_id
                JOIN snapshots s ON s.candidate_id=c.id AND s.run_id=ch.run_id
                WHERE ch.run_id=? AND ch.kind<>'new_candidate'
                ORDER BY ch.id DESC LIMIT 8
                """,
                (latest_run_id,),
            ).fetchall()
            changes = [
                {
                    **json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                    "candidate_id": row["candidate_id"],
                    "hostname": row["hostname"],
                    "company_name": concise_company_name(
                        label_hint=row["label_hint"] or "",
                        title=row["title"] or "",
                        canonical_domain=row["canonical_host"] or "",
                        hostname=row["hostname"],
                    ),
                    "final_score": row["final_score"],
                }
                for row in change_rows
            ]
        sources = self.list_sources()
        source_leaders = sorted(
            sources,
            key=lambda source: (
                int(source["opportunity_score"]),
                int(source["high_priority_count"]),
                int(source["candidate_count"]),
            ),
            reverse=True,
        )[:5]
        indexed_names = sum(int(source["indexed_names"] or 0) for source in sources)
        inspected = int(metrics["total"])
        return {
            "metrics": dict(metrics),
            "pipeline": {row["stage"]: row["count"] for row in pipeline_rows},
            "tags": self.list_tags(),
            "top_prospects": prospect_result["items"],
            "recent_changes": changes,
            "source_leaders": source_leaders,
            "latest_run": latest_run,
            "sources": {
                "total": len(sources),
                "enabled": sum(source["enabled"] for source in sources),
                "indexed_names": indexed_names,
                "estimated_uninspected": max(indexed_names - inspected, 0),
            },
        }

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> EvidenceClaim:
        return EvidenceClaim.model_validate(
            {
                "id": int(row["id"]),
                "account_id": int(row["account_id"]),
                "signals": json.loads(row["signals_json"] or "[]"),
                "gates": json.loads(row["gates_json"] or "[]"),
                "dimensions": json.loads(row["dimensions_json"] or "[]"),
                "claim": row["claim"],
                "epistemic_status": row["epistemic_status"],
                "truth_state": row["truth_state"],
                "source_kind": row["source_kind"],
                "source_url": row["source_url"],
                "observed_at": row["observed_at"],
                "confidence": row["confidence"],
                "human_verified": bool(row["human_verified"]),
                "verified_by": row["verified_by"] or "",
                "verified_at": row["verified_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    def _claims_for_account(self, account_id: int) -> list[EvidenceClaim]:
        rows = self.connection.execute(
            """
            SELECT * FROM account_claims
            WHERE account_id=?
            ORDER BY observed_at DESC, created_at DESC, id DESC
            """,
            (account_id,),
        ).fetchall()
        return [self._claim_from_row(row) for row in rows]

    def _target_intelligence_for_account(
        self,
        *,
        candidate_id: int | None,
        website_host: str | None,
    ) -> dict[str, Any] | None:
        conditions: list[str] = []
        params: list[Any] = []
        if candidate_id is not None:
            conditions.append("c.id=?")
            params.append(candidate_id)
        elif website_host:
            conditions.append("ti.canonical_domain=?")
            params.append(self._normalize_identity_host(website_host))
        else:
            return None
        row = self.connection.execute(
            f"""
            WITH latest AS (
                SELECT candidate_id, MAX(id) AS snapshot_id
                FROM snapshots GROUP BY candidate_id
            )
            SELECT c.id AS target_candidate_id, c.hostname AS target_hostname,
                   c.source_apex AS target_source_apex, s.observed_at,
                   {TARGET_PROJECTION_SELECT}
            FROM latest l
            JOIN candidates c ON c.id=l.candidate_id
            JOIN snapshots s ON s.id=l.snapshot_id
            JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
            WHERE {" AND ".join(conditions)}
            ORDER BY ti.review_ready DESC, ti.target_score DESC,
                     ti.principal_signals_met DESC, ti.data_confidence DESC,
                     s.observed_at DESC, c.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        target = self._target_intelligence_from_row(row)
        if target is not None:
            target.update(
                {
                    "candidate_id": int(row["target_candidate_id"]),
                    "hostname": row["target_hostname"],
                    "source_apex": row["target_source_apex"],
                    "observed_at": row["observed_at"],
                }
            )
        return target

    def _account_from_row(
        self,
        row: sqlite3.Row,
        *,
        include_claims: bool,
    ) -> dict[str, Any]:
        account_id = int(row["id"])
        qualification = StrategicQualification.model_validate_json(
            row["qualification_json"] or "{}"
        )
        capacity_map = CreativeCapacityMap.model_validate_json(row["capacity_map_json"] or "{}")
        discovery = DiscoveryReview.model_validate_json(row["discovery_json"] or "{}")
        outcome = SalesOutcome.model_validate_json(row["outcome_json"] or "{}")
        delivery = DeliveryOutcome.model_validate_json(row["delivery_json"] or "{}")
        claims = self._claims_for_account(account_id) if include_claims else []
        account = SalesAccount(
            id=account_id,
            candidate_id=row["candidate_id"],
            company=row["company"],
            website=row["website"],
            cohort=row["cohort"],
            relationship_type=row["relationship_type"],
            segment=row["segment"],
            lead_source=row["lead_source"],
            referrer=row["referrer"],
            intro_status=row["intro_status"],
            lifecycle=row["lifecycle"],
            qualification=qualification,
            capacity_map=capacity_map,
            discovery=discovery,
            outcome=outcome,
            delivery=delivery,
            claims=claims,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        payload = account.model_dump(mode="json")
        target_intelligence = self._target_intelligence_for_account(
            candidate_id=row["candidate_id"],
            website_host=row["website_host"],
        )
        payload.update(
            {
                "website_host": row["website_host"],
                "notes": row["notes"],
                "qualification_state": qualification.state,
                "qualification_score": qualification.total_score,
                "claim_count": (len(claims) if include_claims else int(row["claim_count"])),
                "delivered_to_used_rate": delivery.delivered_to_used_rate,
                "target_intelligence": target_intelligence,
            }
        )
        return payload

    def _account_row(self, account_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT a.*, (
                SELECT COUNT(*) FROM account_claims c WHERE c.account_id=a.id
            ) AS claim_count
            FROM sales_accounts a WHERE a.id=?
            """,
            (account_id,),
        ).fetchone()

    def _candidate_account_defaults(self, candidate_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT c.id, c.hostname, c.label_hint, s.observation_json, s.score_json, s.ai_json
            FROM candidates c
            LEFT JOIN snapshots s ON s.id=(
                SELECT MAX(latest.id) FROM snapshots latest WHERE latest.candidate_id=c.id
            )
            WHERE c.id=?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        company = concise_company_name(
            label_hint=row["label_hint"] or "",
            hostname=row["hostname"],
        )
        website = f"https://{row['hostname']}"
        segment = "other"
        relationship_type = "buyer"
        observation: SiteObservation | None = None
        if row["observation_json"]:
            observation = SiteObservation.model_validate_json(row["observation_json"])
            company = concise_company_name(
                label_hint=row["label_hint"] or "",
                title=observation.title,
                canonical_domain=observation.canonical_host,
                hostname=row["hostname"],
            )
            website = (
                observation.final_url
                or (f"https://{observation.canonical_host}" if observation.canonical_host else "")
                or website
            )
        if row["ai_json"]:
            ai = AIAnalysis.model_validate_json(row["ai_json"])
            company = concise_company_name(
                ai_name=ai.company_name,
                label_hint=row["label_hint"] or "",
                title=observation.title if observation else "",
                canonical_domain=observation.canonical_host if observation else "",
                hostname=row["hostname"],
            )
            website = ai.company_url or website
        if row["score_json"]:
            score = ScoreCard.model_validate_json(row["score_json"])
            if score.archetype == "agency":
                if score.placement.ready and not score.disqualified:
                    segment = "agency_buyer"
                    relationship_type = "buyer"
                else:
                    segment = "agency_referral_partner"
                    relationship_type = "referral_partner"
            elif score.archetype == "ecommerce_brand":
                segment = "consumer_brand"
        return {
            "company": company,
            "website": website,
            "segment": segment,
            "relationship_type": relationship_type,
        }

    def _prepare_account(
        self,
        *,
        company: str = "",
        website: str = "",
        candidate_id: int | None = None,
        cohort: str = "prototype-v1",
        relationship_type: str | None = None,
        segment: str | None = None,
        lead_source: str = "targeted_outbound",
        referrer: str = "",
        intro_status: str = "none",
        notes: str = "",
    ) -> tuple[SalesAccount, str, str]:
        candidate_defaults: dict[str, Any] = {}
        if candidate_id is not None:
            candidate_defaults = self._candidate_account_defaults(candidate_id) or {}
            if not candidate_defaults:
                raise ValueError("Candidate not found")
        company = re.sub(r"\s+", " ", company.strip() or candidate_defaults.get("company", ""))
        website = website.strip() or candidate_defaults.get("website", "")
        normalized_website, website_host = normalize_account_website(website)
        if not company:
            raise ValueError("Company is required")
        if not normalized_website or not website_host:
            raise ValueError("Website is required")
        relationship = relationship_type or candidate_defaults.get("relationship_type", "buyer")
        account_segment = segment or candidate_defaults.get("segment", "other")
        account = SalesAccount(
            candidate_id=candidate_id,
            company=company[:300],
            website=normalized_website,
            cohort=cohort.strip() or "prototype-v1",
            relationship_type=relationship,
            segment=account_segment,
            lead_source=lead_source,
            referrer=referrer.strip(),
            intro_status=intro_status,
        )
        return account, website_host, notes.strip()

    def _create_account_uncommitted(
        self,
        account: SalesAccount,
        website_host: str,
        notes: str,
    ) -> tuple[int, bool]:
        existing: sqlite3.Row | None = None
        if account.candidate_id is not None:
            existing = self.connection.execute(
                "SELECT id FROM sales_accounts WHERE candidate_id=?",
                (account.candidate_id,),
            ).fetchone()
        if existing is None:
            existing = self.connection.execute(
                "SELECT id, candidate_id FROM sales_accounts WHERE website_host=?",
                (website_host,),
            ).fetchone()
            if existing is not None and account.candidate_id is not None:
                current_candidate = existing["candidate_id"]
                if current_candidate is None:
                    self.connection.execute(
                        "UPDATE sales_accounts SET candidate_id=?, updated_at=? WHERE id=?",
                        (account.candidate_id, _now(), existing["id"]),
                    )
        if existing is not None:
            return int(existing["id"]), False

        timestamp = _now()
        qualification = evaluate_strategic_qualification(account.qualification, [])
        capacity_map = evaluate_capacity_map(account.capacity_map, qualification)
        cursor = self.connection.execute(
            """
            INSERT INTO sales_accounts(
                candidate_id, company, website, website_host, cohort, relationship_type,
                segment, lead_source, referrer, intro_status, notes,
                qualification_json, capacity_map_json, discovery_json, outcome_json,
                delivery_json, qualification_state, qualification_score, lifecycle,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.candidate_id,
                account.company,
                account.website,
                website_host,
                account.cohort,
                account.relationship_type,
                account.segment,
                account.lead_source,
                account.referrer,
                account.intro_status,
                notes,
                qualification.model_dump_json(),
                capacity_map.model_dump_json(),
                account.discovery.model_dump_json(),
                account.outcome.model_dump_json(),
                account.delivery.model_dump_json(),
                qualification.state,
                qualification.total_score,
                account.lifecycle,
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid), True

    def create_account(self, **values: Any) -> tuple[dict[str, Any], bool]:
        account, website_host, notes = self._prepare_account(**values)
        self._admit_company_hosts([website_host])
        require_allowed(website_host)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            account_id, created = self._create_account_uncommitted(account, website_host, notes)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        result = self.account_detail(account_id)
        if result is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("Created account could not be loaded")
        return result, created

    def list_accounts(
        self,
        *,
        cohort: str = "prototype-v1",
        relationship_type: str = "",
        segment: str = "",
        lead_source: str = "",
        qualification_state: str = "",
        lifecycle: str = "",
        min_score: int = 0,
        search: str = "",
        sort: str = "score",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = ["qualification_score>=?"]
        params: list[Any] = [min_score]
        filters = {
            "cohort": cohort,
            "relationship_type": relationship_type,
            "segment": segment,
            "lead_source": lead_source,
            "qualification_state": qualification_state,
            "lifecycle": lifecycle,
        }
        for column, value in filters.items():
            if value:
                conditions.append(f"{column}=?")
                params.append(value)
        if search:
            conditions.append(
                "(lower(company) LIKE ? OR lower(website_host) LIKE ? "
                "OR lower(referrer) LIKE ? OR lower(notes) LIKE ?)"
            )
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle, needle, needle])
        order = {
            "score": "qualification_score DESC, updated_at DESC, id DESC",
            "updated": "updated_at DESC, id DESC",
            "company": "company COLLATE NOCASE, id DESC",
        }.get(sort, "qualification_score DESC, updated_at DESC, id DESC")
        where = " AND ".join(conditions)
        total = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM sales_accounts WHERE {where}", params
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            f"""
            SELECT a.*, (
                SELECT COUNT(*) FROM account_claims c WHERE c.account_id=a.id
            ) AS claim_count
            FROM sales_accounts a
            WHERE {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return {
            "items": [self._account_from_row(row, include_claims=False) for row in rows],
            "total": total,
        }

    def account_detail(self, account_id: int) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        return self._account_from_row(row, include_claims=True)

    def patch_account(self, account_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        allowed = {
            "company",
            "website",
            "cohort",
            "relationship_type",
            "segment",
            "lead_source",
            "referrer",
            "intro_status",
            "notes",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"Unsupported account profile fields: {', '.join(unknown)}")
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            return self._account_from_row(row, include_claims=True)

        profile = SalesAccount(
            id=account_id,
            candidate_id=row["candidate_id"],
            company=updates.get("company", row["company"]),
            website=updates.get("website", row["website"]),
            cohort=updates.get("cohort", row["cohort"]),
            relationship_type=updates.get("relationship_type", row["relationship_type"]),
            segment=updates.get("segment", row["segment"]),
            lead_source=updates.get("lead_source", row["lead_source"]),
            referrer=updates.get("referrer", row["referrer"]),
            intro_status=updates.get("intro_status", row["intro_status"]),
            lifecycle=row["lifecycle"],
            qualification=StrategicQualification.model_validate_json(
                row["qualification_json"] or "{}"
            ),
            capacity_map=CreativeCapacityMap.model_validate_json(row["capacity_map_json"] or "{}"),
            discovery=DiscoveryReview.model_validate_json(row["discovery_json"] or "{}"),
            outcome=SalesOutcome.model_validate_json(row["outcome_json"] or "{}"),
            delivery=DeliveryOutcome.model_validate_json(row["delivery_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=_now(),
        )
        normalized_website, website_host = normalize_account_website(profile.website)
        self._admit_company_hosts([website_host])
        require_allowed(website_host)
        profile.website = normalized_website
        company = re.sub(r"\s+", " ", profile.company.strip())
        if not company:
            raise ValueError("Company is required")
        timestamp = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE sales_accounts
                SET company=?, website=?, website_host=?, cohort=?, relationship_type=?,
                    segment=?, lead_source=?, referrer=?, intro_status=?, notes=?,
                    lifecycle=?, updated_at=?
                WHERE id=?
                """,
                (
                    company[:300],
                    profile.website,
                    website_host,
                    profile.cohort.strip() or "prototype-v1",
                    profile.relationship_type,
                    profile.segment,
                    profile.lead_source,
                    profile.referrer.strip(),
                    profile.intro_status,
                    str(updates.get("notes", row["notes"])).strip(),
                    profile.lifecycle,
                    timestamp,
                    account_id,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise AccountConflictError("An account already uses that website") from exc
        except Exception:
            self.connection.rollback()
            raise
        return self.account_detail(account_id)

    def _recompute_account_uncommitted(self, account_id: int) -> None:
        row = self._account_row(account_id)
        if row is None:
            raise ValueError("Account not found")
        claims = self._claims_for_account(account_id)
        qualification = evaluate_strategic_qualification(
            StrategicQualification.model_validate_json(row["qualification_json"] or "{}"),
            claims,
        )
        capacity_map = evaluate_capacity_map(
            CreativeCapacityMap.model_validate_json(row["capacity_map_json"] or "{}"),
            qualification,
        )
        lifecycle = str(row["lifecycle"])
        if lifecycle == "research" and qualification.state == "pursue":
            lifecycle = "qualified"
        elif lifecycle == "qualified" and qualification.state != "pursue":
            lifecycle = "research"
        self.connection.execute(
            """
            UPDATE sales_accounts
            SET qualification_json=?, capacity_map_json=?, qualification_state=?,
                qualification_score=?, lifecycle=?, updated_at=?
            WHERE id=?
            """,
            (
                qualification.model_dump_json(),
                capacity_map.model_dump_json(),
                qualification.state,
                qualification.total_score,
                lifecycle,
                _now(),
                account_id,
            ),
        )

    def save_qualification(
        self,
        account_id: int,
        qualification: StrategicQualification,
    ) -> dict[str, Any] | None:
        if self._account_row(account_id) is None:
            return None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            claims = self._claims_for_account(account_id)
            evaluated = evaluate_strategic_qualification(qualification, claims)
            self.connection.execute(
                "UPDATE sales_accounts SET qualification_json=? WHERE id=?",
                (evaluated.model_dump_json(), account_id),
            )
            self._recompute_account_uncommitted(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.account_detail(account_id)

    def list_claims(self, account_id: int) -> list[dict[str, Any]] | None:
        if self._account_row(account_id) is None:
            return None
        return [claim.model_dump(mode="json") for claim in self._claims_for_account(account_id)]

    @staticmethod
    def _claim_values(claim: EvidenceClaim) -> tuple[Any, ...]:
        return (
            json.dumps(claim.signals),
            json.dumps(claim.gates),
            json.dumps(claim.dimensions),
            claim.claim.strip(),
            claim.epistemic_status,
            claim.truth_state,
            claim.source_kind,
            claim.source_url,
            claim.observed_at.isoformat(),
            claim.confidence,
            int(claim.human_verified),
            claim.verified_by.strip(),
            claim.verified_at.isoformat() if claim.verified_at else None,
        )

    def create_claim(
        self,
        account_id: int,
        claim: EvidenceClaim,
    ) -> dict[str, Any] | None:
        if self._account_row(account_id) is None:
            return None
        timestamp = _now()
        normalized = EvidenceClaim.model_validate(
            {
                **claim.model_dump(),
                "id": None,
                "account_id": account_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO account_claims(
                    account_id, signals_json, gates_json, dimensions_json, claim,
                    epistemic_status, truth_state, source_kind, source_url, observed_at,
                    confidence, human_verified, verified_by, verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    *self._claim_values(normalized),
                    timestamp,
                    timestamp,
                ),
            )
            claim_id = int(cursor.lastrowid)
            self._recompute_account_uncommitted(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        row = self.connection.execute(
            "SELECT * FROM account_claims WHERE id=? AND account_id=?",
            (claim_id, account_id),
        ).fetchone()
        return self._claim_from_row(row).model_dump(mode="json") if row else None

    def update_claim(
        self,
        account_id: int,
        claim_id: int,
        claim: EvidenceClaim,
    ) -> dict[str, Any] | None:
        existing = self.connection.execute(
            "SELECT * FROM account_claims WHERE id=? AND account_id=?",
            (claim_id, account_id),
        ).fetchone()
        if existing is None:
            return None
        normalized = EvidenceClaim.model_validate(
            {
                **claim.model_dump(),
                "id": claim_id,
                "account_id": account_id,
                "created_at": existing["created_at"],
                "updated_at": _now(),
            }
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE account_claims
                SET signals_json=?, gates_json=?, dimensions_json=?, claim=?,
                    epistemic_status=?, truth_state=?, source_kind=?, source_url=?,
                    observed_at=?, confidence=?, human_verified=?, verified_by=?,
                    verified_at=?, updated_at=?
                WHERE id=? AND account_id=?
                """,
                (
                    *self._claim_values(normalized),
                    normalized.updated_at.isoformat(),
                    claim_id,
                    account_id,
                ),
            )
            self._recompute_account_uncommitted(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        row = self.connection.execute(
            "SELECT * FROM account_claims WHERE id=? AND account_id=?",
            (claim_id, account_id),
        ).fetchone()
        return self._claim_from_row(row).model_dump(mode="json") if row else None

    def delete_claim(self, account_id: int, claim_id: int) -> bool | None:
        if self._account_row(account_id) is None:
            return None
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                "DELETE FROM account_claims WHERE id=? AND account_id=?",
                (claim_id, account_id),
            )
            if not cursor.rowcount:
                self.connection.rollback()
                return False
            self._recompute_account_uncommitted(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return True

    def save_capacity_map(
        self,
        account_id: int,
        capacity_map: CreativeCapacityMap,
    ) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        qualification = StrategicQualification.model_validate_json(
            row["qualification_json"] or "{}"
        )
        evaluated = evaluate_capacity_map(capacity_map, qualification)
        self.connection.execute(
            "UPDATE sales_accounts SET capacity_map_json=?, updated_at=? WHERE id=?",
            (evaluated.model_dump_json(), _now(), account_id),
        )
        self.connection.commit()
        return self.account_detail(account_id)

    def _record_account_event_uncommitted(
        self, account_id: int, event_type: str, previous: str, current: str,
    ) -> None:
        """Keep an append-only record of operator facts independently of qualification."""
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS sales_account_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES sales_accounts(id),
                event_type TEXT NOT NULL, previous_json TEXT NOT NULL,
                current_json TEXT NOT NULL, recorded_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """INSERT INTO sales_account_events(
                account_id, event_type, previous_json, current_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (account_id, event_type, previous, current, _now()),
        )

    def save_discovery(
        self,
        account_id: int,
        discovery: DiscoveryReview,
    ) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        discovery = DiscoveryReview.model_validate(discovery.model_dump())
        current = DiscoveryReview.model_validate_json(row["discovery_json"] or "{}")
        if current.model_dump() == discovery.model_dump():
            return self.account_detail(account_id)
        lifecycle = str(row["lifecycle"])
        if discovery.completed:
            # A held conversation is a fact even when it disproves the buying hypothesis.
            # Three asks, a booked-meeting record and qualification are proposal gates,
            # not prerequisites for recording what was learned.
            if lifecycle in {"research", "qualified", "outreach", "discovery"}:
                lifecycle = "discovery"
        elif current.completed:
            if lifecycle in {"proposal", "won", "lost", "paused"}:
                raise AccountConflictError(
                    f"Completed discovery cannot be reopened while lifecycle is {lifecycle}"
                )
            lifecycle = "outreach"
        try:
            self.connection.execute(
                """UPDATE sales_accounts
                SET discovery_json=?, lifecycle=?, updated_at=? WHERE id=?""",
                (discovery.model_dump_json(), lifecycle, _now(), account_id),
            )
            if current.model_dump() != discovery.model_dump():
                self._record_account_event_uncommitted(
                    account_id, "discovery", current.model_dump_json(), discovery.model_dump_json(),
                )
            from .sales_engine.persistence import SalesEngineRepository
            engine = SalesEngineRepository(self.connection)
            engine.initialize_schema()
            engine.capture_account_state(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.account_detail(account_id)

    @staticmethod
    def _validate_outreach_transition(current: str, target: str) -> None:
        transitions = {
            "not_contacted": {"contacted", "replied", "meeting_booked"},
            "contacted": {"replied", "meeting_booked", "no_response"},
            "replied": {"meeting_booked", "no_response"},
            "meeting_booked": set(),
            "no_response": {"contacted", "replied", "meeting_booked"},
        }
        if target != current and target not in transitions.get(current, set()):
            raise AccountConflictError(f"Cannot change outreach outcome from {current} to {target}")

    @staticmethod
    def _validate_proposal_transition(current: str, target: str) -> None:
        transitions = {
            "not_sent": {"pending"},
            "pending": {"won", "lost"},
            "won": set(),
            "lost": set(),
        }
        if target != current and target not in transitions.get(current, set()):
            raise AccountConflictError(f"Cannot change proposal outcome from {current} to {target}")

    def save_outcome(
        self,
        account_id: int,
        outcome: SalesOutcome,
    ) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        outcome = SalesOutcome.model_validate(outcome.model_dump())
        current = SalesOutcome.model_validate_json(row["outcome_json"] or "{}")
        if current.model_dump() == outcome.model_dump():
            return self.account_detail(account_id)
        qualification = StrategicQualification.model_validate_json(
            row["qualification_json"] or "{}"
        )
        capacity_map = CreativeCapacityMap.model_validate_json(row["capacity_map_json"] or "{}")
        discovery = DiscoveryReview.model_validate_json(row["discovery_json"] or "{}")
        self._validate_outreach_transition(current.outreach_outcome, outcome.outreach_outcome)
        self._validate_proposal_transition(current.proposal_outcome, outcome.proposal_outcome)
        proposal_advancing = outcome.proposal_outcome != current.proposal_outcome
        if proposal_advancing and outcome.proposal_outcome in {"pending", "won"}:
            if (discovery.outcome in {"not_fit", "no_budget", "not_now"}
                    or discovery.confirmed_recurring_need is False):
                raise AccountConflictError(
                    "Resolve the negative discovery outcome before advancing a proposal"
                )
            if qualification.state != "pursue":
                raise AccountConflictError("Advancing a proposal requires Pursue qualification")
            if not capacity_map.ready:
                raise AccountConflictError(
                    "Advancing a proposal requires a ready Creative Capacity Map"
                )
            if not discovery.completed:
                raise AccountConflictError(
                    "A completed discovery review is required before proposal"
                )
        if outcome.month_one_started_at and outcome.proposal_outcome != "won":
            raise AccountConflictError("Month 1 can start only after a won proposal")
        lifecycle = str(row["lifecycle"])
        if outcome.proposal_outcome == "pending":
            lifecycle = "proposal"
        elif outcome.proposal_outcome in {"won", "lost"}:
            lifecycle = outcome.proposal_outcome
        elif outcome.outreach_outcome != "not_contacted" and lifecycle in {"research", "qualified"}:
            lifecycle = "outreach"
        try:
            self.connection.execute(
                """
                UPDATE sales_accounts
                SET outcome_json=?, lifecycle=?, updated_at=? WHERE id=?
                """,
                (outcome.model_dump_json(), lifecycle, _now(), account_id),
            )
            if current.model_dump() != outcome.model_dump():
                self._record_account_event_uncommitted(
                    account_id, "outcome", current.model_dump_json(), outcome.model_dump_json(),
                )
            if (
                current.outreach_outcome == "not_contacted"
                and outcome.outreach_outcome != "not_contacted"
                and row["candidate_id"] is not None
            ):
                timestamp = _now()
                activity = self.connection.execute(
                    """INSERT INTO prospect_activities(
                        candidate_id, activity_type, summary, contact_email, occurred_at, created_at
                    ) VALUES (?, 'outreach_recorded', ?, '', ?, ?)""",
                    (row["candidate_id"], f"Operator recorded {outcome.outreach_outcome}",
                     timestamp, timestamp),
                )
                self._record_intelligence_exposure_uncommitted(
                    candidate_id=int(row["candidate_id"]), activity_id=int(activity.lastrowid),
                    channel="outreach_recorded", occurred_at=timestamp, created_at=timestamp,
                )
            from .sales_engine.persistence import SalesEngineRepository
            engine = SalesEngineRepository(self.connection)
            engine.initialize_schema()
            engine.capture_account_state(account_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.account_detail(account_id)

    def save_delivery(
        self,
        account_id: int,
        delivery: DeliveryOutcome,
    ) -> dict[str, Any] | None:
        row = self._account_row(account_id)
        if row is None:
            return None
        outcome = SalesOutcome.model_validate_json(row["outcome_json"] or "{}")
        if outcome.proposal_outcome != "won" or outcome.month_one_started_at is None:
            raise AccountConflictError(
                "Delivery metrics require a won proposal with a Month 1 start date"
            )
        if delivery.renewal_decision != "pending" and delivery.renewal_decided_at is None:
            raise AccountConflictError("A renewal decision requires renewal_decided_at")
        if delivery.renewal_decision == "pending" and delivery.renewal_decided_at is not None:
            raise AccountConflictError("renewal_decided_at must be empty while renewal is pending")
        self.connection.execute(
            "UPDATE sales_accounts SET delivery_json=?, updated_at=? WHERE id=?",
            (delivery.model_dump_json(), _now(), account_id),
        )
        from .sales_engine.persistence import SalesEngineRepository
        engine = SalesEngineRepository(self.connection)
        engine.initialize_schema()
        engine.capture_account_state(account_id)
        self.connection.commit()
        return self.account_detail(account_id)

    def import_accounts(
        self,
        rows: list[dict[str, Any]],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        if len(rows) > MAX_ACCOUNT_IMPORT_ROWS:
            raise ValueError(f"Account import is limited to {MAX_ACCOUNT_IMPORT_ROWS} rows")
        prepared: list[tuple[int, SalesAccount, str, str]] = []
        row_results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for number, values in enumerate(rows, start=2):
            try:
                account, website_host, notes = self._prepare_account(**values)
                prepared.append((number, account, website_host, notes))
            except (TypeError, ValueError) as exc:
                error = {"row": number, "error": str(exc)}
                errors.append(error)
                row_results.append({**error, "action": "error"})

        if errors:
            return {
                "dry_run": dry_run,
                "valid": False,
                "total": len(rows),
                "created": 0,
                "existing": 0,
                "rows": sorted(row_results, key=lambda item: item["row"]),
                "errors": errors,
            }

        created_count = 0
        existing_count = 0
        if dry_run:
            seen_hosts: dict[str, int | None] = {}
            seen_candidates: dict[int, int | None] = {}
            for number, account, website_host, _notes in prepared:
                existing = None
                if account.candidate_id is not None:
                    existing = self.connection.execute(
                        "SELECT id FROM sales_accounts WHERE candidate_id=?",
                        (account.candidate_id,),
                    ).fetchone()
                if existing is None:
                    existing = self.connection.execute(
                        "SELECT id FROM sales_accounts WHERE website_host=?",
                        (website_host,),
                    ).fetchone()
                duplicate_in_batch = website_host in seen_hosts or (
                    account.candidate_id is not None and account.candidate_id in seen_candidates
                )
                action = "existing" if existing is not None or duplicate_in_batch else "create"
                account_id = int(existing["id"]) if existing is not None else None
                row_results.append(
                    {
                        "row": number,
                        "action": action,
                        "account_id": account_id,
                        "company": account.company,
                        "website": account.website,
                        "website_host": website_host,
                    }
                )
                if action == "create":
                    created_count += 1
                else:
                    existing_count += 1
                seen_hosts[website_host] = account_id
                if account.candidate_id is not None:
                    seen_candidates[account.candidate_id] = account_id
        else:
            self._admit_company_hosts(host for _, _, host, _ in prepared)
            denied = [{"row": number, "error": "Company domain is disallowed"}
                      for number, _, host, _ in prepared if is_disallowed(host)]
            if denied:
                return {"dry_run": False, "valid": False, "total": len(rows),
                        "created": 0, "existing": 0, "rows": denied, "errors": denied}
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for number, account, website_host, notes in prepared:
                    account_id, created = self._create_account_uncommitted(
                        account, website_host, notes
                    )
                    created_count += int(created)
                    existing_count += int(not created)
                    row_results.append(
                        {
                            "row": number,
                            "action": "created" if created else "existing",
                            "account_id": account_id,
                            "company": account.company,
                            "website": account.website,
                            "website_host": website_host,
                        }
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return {
            "dry_run": dry_run,
            "valid": True,
            "total": len(rows),
            "created": created_count,
            "existing": existing_count,
            "rows": sorted(row_results, key=lambda item: item["row"]),
            "errors": [],
        }

    def account_dashboard(self, *, cohort: str = "prototype-v1") -> dict[str, Any]:
        result = self.list_accounts(cohort=cohort, limit=10_000, sort="score")
        accounts = result["items"]
        qualification = {state: 0 for state in ("pursue", "needs_research", "disqualified")}
        pipeline = {
            stage: 0
            for stage in (
                "research",
                "qualified",
                "outreach",
                "discovery",
                "proposal",
                "won",
                "lost",
                "paused",
            )
        }
        segments: dict[str, int] = {}
        lead_sources: dict[str, int] = {}
        wins_by_source: dict[str, int] = {}
        relationships: dict[str, int] = {}
        discovery_calls = 0
        wins = 0
        losses = 0
        proposals = 0
        renewals = 0
        nonrenewals = 0
        assets_delivered = 0
        assets_used = 0
        revenue = 0.0
        delivery_cost = 0.0
        margin_observations = 0
        delivery_samples = 0
        revision_rounds = 0
        revision_hours = 0.0
        internal_hours_displaced = 0.0
        vendor_hours_displaced = 0.0
        cycle_time_reductions: list[float] = []
        delivery_burden_counts = {
            burden: 0 for burden in ("unknown", "low", "medium", "high", "uneconomic")
        }
        onboarding_this_week = 0
        current_iso_week = datetime.now(UTC).date().isocalendar()[:2]
        for account in accounts:
            qualification[account["qualification_state"]] += 1
            pipeline[account["lifecycle"]] += 1
            segments[account["segment"]] = segments.get(account["segment"], 0) + 1
            lead_sources[account["lead_source"]] = lead_sources.get(account["lead_source"], 0) + 1
            relationships[account["relationship_type"]] = (
                relationships.get(account["relationship_type"], 0) + 1
            )
            discovery_calls += int(account["discovery"]["completed"])
            proposal_outcome = account["outcome"]["proposal_outcome"]
            proposals += int(proposal_outcome != "not_sent")
            wins += int(proposal_outcome == "won")
            losses += int(proposal_outcome == "lost")
            if proposal_outcome == "won":
                source = account["lead_source"]
                wins_by_source[source] = wins_by_source.get(source, 0) + 1
            month_one_started_at = account["outcome"]["month_one_started_at"]
            if month_one_started_at:
                started = date.fromisoformat(month_one_started_at)
                onboarding_this_week += int(started.isocalendar()[:2] == current_iso_week)
            delivery = account["delivery"]
            renewal = delivery["renewal_decision"]
            renewals += int(renewal == "renewed")
            nonrenewals += int(renewal == "not_renewed")
            assets_delivered += int(delivery["assets_delivered"])
            assets_used += int(delivery["assets_used"])
            recognized = delivery["recognized_revenue"]
            cost = delivery["direct_delivery_cost"]
            if recognized is not None and cost is not None:
                revenue += float(recognized)
                delivery_cost += float(cost)
                margin_observations += 1
            delivery_observed = proposal_outcome == "won" and bool(
                delivery["time_to_specialist_match_hours"] is not None
                or delivery["time_to_first_useful_draft_hours"] is not None
                or delivery["time_to_first_activated_asset_hours"] is not None
                or delivery["cycle_time_reduction_percent"] is not None
                or delivery["assets_delivered"]
                or delivery["assets_used"]
                or delivery["revision_rounds"]
                or delivery["revision_hours"]
                or delivery["internal_hours_displaced"]
                or delivery["vendor_coordination_hours_displaced"]
                or delivery["month_two_queue_depth"]
                or recognized is not None
                or cost is not None
                or delivery["delivery_burden"] != "unknown"
                or delivery["day_21_renewal_intent"] != "unknown"
                or renewal != "pending"
            )
            if delivery_observed:
                delivery_samples += 1
                revision_rounds += int(delivery["revision_rounds"])
                revision_hours += float(delivery["revision_hours"])
                internal_hours_displaced += float(delivery["internal_hours_displaced"])
                vendor_hours_displaced += float(delivery["vendor_coordination_hours_displaced"])
                delivery_burden_counts[delivery["delivery_burden"]] += 1
                if delivery["cycle_time_reduction_percent"] is not None:
                    cycle_time_reductions.append(float(delivery["cycle_time_reduction_percent"]))
        decided_sales = wins + losses
        decided_renewals = renewals + nonrenewals
        core_segments = {
            "consumer_brand",
            "consumer_app",
            "digital_product",
            "gaming",
            "subscription_business",
        }
        paid_growth_consumer_digital = sum(
            count for segment, count in segments.items() if segment in core_segments
        )
        agency_buyers = segments.get("agency_buyer", 0)
        return {
            "cohort": cohort,
            "metrics": {
                "total": len(accounts),
                "pursue": qualification["pursue"],
                "discovery_calls": discovery_calls,
                "proposals": proposals,
                "wins": wins,
                "losses": losses,
                "close_rate": round(wins / decided_sales, 4) if decided_sales else None,
                "renewals": renewals,
                "nonrenewals": nonrenewals,
                "renewal_rate": (
                    round(renewals / decided_renewals, 4) if decided_renewals else None
                ),
                "assets_delivered": assets_delivered,
                "assets_used": assets_used,
                "delivered_to_used_rate": (
                    round(assets_used / assets_delivered, 4) if assets_delivered else None
                ),
                "recognized_revenue": round(revenue, 2),
                "direct_delivery_cost": round(delivery_cost, 2),
                "contribution_margin": (
                    round(revenue - delivery_cost, 2) if margin_observations else None
                ),
                "delivery_sample_size": delivery_samples,
                "average_revision_rounds": (
                    round(revision_rounds / delivery_samples, 2) if delivery_samples else None
                ),
                "average_revision_hours": (
                    round(revision_hours / delivery_samples, 2) if delivery_samples else None
                ),
                "onboarding_this_week": onboarding_this_week,
            },
            "qualification": qualification,
            "pipeline": pipeline,
            "segments": segments,
            "lead_sources": lead_sources,
            "relationships": relationships,
            "outcomes": {
                "delivery_sample_size": delivery_samples,
                "average_revision_rounds": (
                    round(revision_rounds / delivery_samples, 2) if delivery_samples else None
                ),
                "average_revision_hours": (
                    round(revision_hours / delivery_samples, 2) if delivery_samples else None
                ),
                "average_internal_hours_displaced": (
                    round(internal_hours_displaced / delivery_samples, 2)
                    if delivery_samples
                    else None
                ),
                "average_vendor_coordination_hours_displaced": (
                    round(vendor_hours_displaced / delivery_samples, 2)
                    if delivery_samples
                    else None
                ),
                "average_cycle_time_reduction_percent": (
                    round(sum(cycle_time_reductions) / len(cycle_time_reductions), 2)
                    if cycle_time_reductions
                    else None
                ),
                "average_contribution_margin": (
                    round((revenue - delivery_cost) / margin_observations, 2)
                    if margin_observations
                    else None
                ),
                "delivery_burden_counts": delivery_burden_counts,
            },
            "founding_mix": {
                "paid_growth_consumer_digital": paid_growth_consumer_digital,
                "agency_buyers": agency_buyers,
                "primary_buyers": paid_growth_consumer_digital + agency_buyers,
                "saas_ai_adjacency": segments.get("saas_ai_adjacency", 0),
                "target_core_minimum": 7,
                "saas_ai_maximum": 3,
            },
            "source_plan": {
                "warm_network": {
                    "accounts": lead_sources.get("past_client", 0)
                    + lead_sources.get("founder_investor_network", 0),
                    "wins": wins_by_source.get("past_client", 0)
                    + wins_by_source.get("founder_investor_network", 0),
                    "target_accounts": 60,
                    "target_wins": 5,
                },
                "specialist_referral": {
                    "accounts": lead_sources.get("specialist_referral", 0),
                    "wins": wins_by_source.get("specialist_referral", 0),
                    "target_accounts": 40,
                    "target_wins": 3,
                },
                "targeted_outbound": {
                    "accounts": lead_sources.get("targeted_outbound", 0),
                    "wins": wins_by_source.get("targeted_outbound", 0),
                    "target_accounts": 150,
                    "target_wins": 2,
                },
                "assumption_notice": "Planning assumptions, not industry benchmarks",
            },
            "onboarding": {
                "started_this_week": onboarding_this_week,
                "weekly_maximum": 2,
                "at_capacity": onboarding_this_week >= 2,
                "over_capacity": onboarding_this_week > 2,
            },
            "calibration": {
                "available": discovery_calls >= 20 and decided_renewals >= 3,
                "discovery_calls": discovery_calls,
                "renewal_decisions": decided_renewals,
                "requires_discovery_calls": 20,
                "requires_renewal_decisions": 3,
                "optimize_on": [
                    "close_rate",
                    "renewal_rate",
                    "revision_burden",
                    "delivery_burden",
                    "contribution_margin",
                ],
                "excluded_metric": "reply_rate",
            },
            "top_accounts": accounts[:10],
        }

    @staticmethod
    def _markdown_text(value: object) -> str:
        text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\\", "\\\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )

    def capacity_map_markdown(self, account_id: int) -> str | None:
        account = self.account_detail(account_id)
        if account is None:
            return None
        capacity_map = account["capacity_map"]
        buyer = capacity_map.get("economic_buyer") or account["qualification"].get("buyer")
        lines = [
            f"# Creative Capacity Map — {self._markdown_text(account['company'])}",
            "",
            f"Website: {self._markdown_text(account['website'])}",
            f"Cohort: {self._markdown_text(account['cohort'])}",
            (
                "Qualification: "
                f"{account['qualification_state']} ({account['qualification_score']}/100)"
            ),
            f"Map ready: {'yes' if capacity_map['ready'] else 'no'}",
            "",
            "## Current promotion",
            "",
        ]
        lines.extend(f"- {self._markdown_text(item)}" for item in capacity_map["current_promotion"])
        if not capacity_map["current_promotion"]:
            lines.append("- Unknown")
        lines.extend(
            [
                "",
                "## Creative pattern",
                "",
                "Distinct concepts: "
                + (
                    str(capacity_map["distinct_concept_count"])
                    if capacity_map["distinct_concept_count"] is not None
                    else "Unknown"
                ),
            ]
        )
        lines.extend(f"- {self._markdown_text(item)}" for item in capacity_map["concept_notes"])
        lines.extend(["", "## Observed demand", ""])
        lines.extend(f"- {self._markdown_text(item)}" for item in capacity_map["observed_demand"])
        if not capacity_map["observed_demand"]:
            lines.append("- Unknown")
        lines.extend(
            [
                "",
                "## Production-gap hypothesis",
                "",
                self._markdown_text(capacity_map["production_gap_hypothesis"]) or "Unknown",
                "",
                "## Three first-month asks",
                "",
            ]
        )
        lines.extend(
            f"{number}. {self._markdown_text(item)}"
            for number, item in enumerate(capacity_map["first_month_asks"], start=1)
        )
        if not capacity_map["first_month_asks"]:
            lines.append("1. Unknown")
        lines.extend(["", "## Proposed first-month queue", ""])
        lines.extend(f"- {self._markdown_text(item)}" for item in capacity_map["first_month_queue"])
        if not capacity_map["first_month_queue"]:
            lines.append("- Unknown")
        lines.extend(
            [
                "",
                "## Buyer and rights",
                "",
                (
                    "Economic buyer: "
                    + (
                        f"{self._markdown_text(buyer.get('name'))} — "
                        f"{self._markdown_text(buyer.get('role'))}"
                        if buyer
                        else "Unknown"
                    )
                ),
                "Likely rights / authenticity / AI objection: "
                + (self._markdown_text(capacity_map["rights_ai_objection"]) or "Unknown"),
                "",
                "## Evidence ledger",
                "",
                "Observed facts and hypotheses are kept separate. Human verification is explicit.",
                "",
            ]
        )
        for heading, statuses in (
            ("Observed facts", {"observed"}),
            ("Hypotheses and unknowns", {"inferred", "unknown"}),
        ):
            lines.extend([f"### {heading}", ""])
            selected = [
                claim for claim in account["claims"] if claim["epistemic_status"] in statuses
            ]
            if not selected:
                lines.append("- None recorded")
            for claim in selected:
                source = self._markdown_text(claim.get("source_url") or claim["source_kind"])
                coverage = (
                    ", ".join([*claim.get("signals", []), *claim.get("gates", [])])
                    or "supporting evidence"
                )
                lines.append(
                    f"- **{claim['truth_state']} · {claim['confidence']} confidence"
                    f" · {'verified' if claim['human_verified'] else 'unverified'}** — "
                    f"{self._markdown_text(claim['claim'])} "
                    f"({self._markdown_text(coverage)}; {claim['observed_at']}; {source})"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def account_pipeline_markdown(self, *, cohort: str = "prototype-v1") -> str:
        dashboard = self.account_dashboard(cohort=cohort)
        metrics = dashboard["metrics"]
        lines = [
            f"# Beings curated pipeline — {self._markdown_text(cohort)}",
            "",
            "This report contains curated sales accounts only; scanner inventory is excluded.",
            "",
            f"- Accounts: {metrics['total']}",
            f"- Pursue: {metrics['pursue']}",
            f"- Completed discovery calls: {metrics['discovery_calls']}",
            f"- Proposals: {metrics['proposals']}",
            f"- Wins / losses: {metrics['wins']} / {metrics['losses']}",
            f"- Renewals / non-renewals: {metrics['renewals']} / {metrics['nonrenewals']}",
            "",
            "## Lifecycle",
            "",
        ]
        lines.extend(f"- {stage}: {count}" for stage, count in dashboard["pipeline"].items())
        lines.extend(["", "## Accounts", ""])
        accounts = self.list_accounts(cohort=cohort, limit=10_000)["items"]
        for account in accounts:
            lines.append(
                f"- {self._markdown_text(account['company'])} — "
                f"{account['qualification_state']} {account['qualification_score']}/100; "
                f"{account['lifecycle']}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def get_ranked(self, run_id: int | None = None, limit: int = 100) -> list[RankedProspect]:
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return []
        rows = self.connection.execute(
            f"""
            WITH ranked AS (
                SELECT c.id AS candidate_id, c.hostname, c.source_apex, c.label_hint,
                       c.first_seen,
                       c.discovery_evidence_json, c.provider_record_ids_json,
                       c.discovery_rank, s.observation_json, s.score_json, s.ai_json,
                       s.final_score, {TARGET_PROJECTION_SELECT},
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(
                               NULLIF(
                                   lower(
                                       trim(
                                           json_extract(s.observation_json, '$.canonical_host')
                                       )
                                   ),
                                   ''
                               ),
                               lower(c.hostname)
                           )
                           ORDER BY MAX(
                               {_research_priority_sql("s")}, COALESCE(ti.target_score, 0)
                           ) DESC,
                           s.final_score DESC, c.discovery_rank DESC, c.id DESC
                       ) AS identity_rank
                FROM snapshots s
                JOIN candidates c ON c.id=s.candidate_id
                LEFT JOIN prospect_workflow w ON w.candidate_id=c.id
                LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id
                WHERE s.run_id=? AND COALESCE(w.excluded, 0)=0
            )
            SELECT candidate_id, hostname, source_apex, label_hint, first_seen,
                   discovery_evidence_json, provider_record_ids_json,
                   observation_json, score_json, ai_json, final_score,
                   target_rubric_version, target_canonical_domain,
                   target_relationship_lane, target_scope, target_archetype,
                   target_score, target_principal_signals_met,
                   target_review_ready, target_data_confidence, target_live,
                   target_disqualified, target_hypothesis_json, target_computed_at
            FROM ranked
            WHERE identity_rank=1
            ORDER BY MAX(
                         {_research_priority_sql("ranked")}, COALESCE(target_score, 0)
                     ) DESC,
                     final_score DESC, discovery_rank DESC, hostname
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        prospects: list[RankedProspect] = []
        for row in rows:
            change_rows = self.connection.execute(
                "SELECT details_json FROM changes WHERE candidate_id=? AND run_id=? ORDER BY id",
                (row["candidate_id"], run_id),
            ).fetchall()
            score = ScoreCard.model_validate_json(row["score_json"])
            target_intelligence = self._target_intelligence_from_row(row)
            if target_intelligence is not None:
                score.creative_capacity = CreativeCapacityHypothesis.model_validate(
                    target_intelligence["hypothesis"]
                )
            prospects.append(
                RankedProspect(
                    candidate_id=row["candidate_id"],
                    hostname=row["hostname"],
                    source_apex=row["source_apex"],
                    label_hint=row["label_hint"] or "",
                    first_seen=row["first_seen"],
                    discovery_evidence_urls=json.loads(row["discovery_evidence_json"] or "[]"),
                    provider_record_ids=json.loads(row["provider_record_ids_json"] or "[]"),
                    observation=SiteObservation.model_validate_json(row["observation_json"]),
                    score=score,
                    ai=AIAnalysis.model_validate_json(row["ai_json"]) if row["ai_json"] else None,
                    final_score=row["final_score"],
                    changes=[json.loads(change["details_json"]) for change in change_rows],
                )
            )
        return prospects

    def run_metadata(self, run_id: int | None = None) -> dict[str, Any]:
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return {}
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        errors = self.connection.execute(
            "SELECT source_apex, error FROM source_errors WHERE run_id=?", (run_id,)
        ).fetchall()
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "config": json.loads(row["config_json"]),
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else {},
            "source_errors": [dict(error) for error in errors],
        }

    def stats(self) -> dict[str, Any]:
        def count(table: str) -> int:
            return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        latest = self.latest_run_id()
        return {
            "database": str(self.path),
            "runs": count("runs"),
            "candidates": count("candidates"),
            "snapshots": count("snapshots"),
            "changes": count("changes"),
            "latest_run_id": latest,
            "latest_run": self.run_metadata(latest) if latest else None,
        }
