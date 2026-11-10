from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import PLATFORM_PROFILES
from .models import DiscoveredHost, SiteObservation

APEX_CIRCUIT_THRESHOLD = 25
APEX_CIRCUIT_COOLDOWN_HOURS = 24
PLATFORM_BLOCKED_STATUS_CODES = frozenset({401, 403, 515})
NOT_PUBLIC_PLATFORM_STATUS_CODES = frozenset({400, 404, 410, 423})
TERMINAL_FETCH_OUTCOMES = frozenset(
    {
        "platform_blocked",
        "not_public_storefront",
        "retired_hostname",
        "circuit_open",
    }
)
GENERIC_TENANT_TOKENS = frozenset(
    {
        "agency",
        "creative",
        "dashboard",
        "digital",
        "marketing",
        "media",
        "mymedia",
        "portal",
        "report",
        "reports",
        "studio",
        "studios",
    }
)


def is_platform_tenant(candidate: DiscoveredHost) -> bool:
    apex = candidate.source_apex.strip().lower().rstrip(".")
    hostname = candidate.hostname.strip().lower().rstrip(".")
    return apex in PLATFORM_PROFILES and hostname.endswith(f".{apex}")


def platform_tenant_label(candidate: DiscoveredHost) -> str:
    if not is_platform_tenant(candidate):
        return ""
    label = " ".join(candidate.label_hint.split()).strip()
    if label:
        return label[:160]
    apex = candidate.source_apex.strip().lower().rstrip(".")
    hostname = candidate.hostname.strip().lower().rstrip(".")
    return hostname[: -(len(apex) + 1)].split(".")[0][:160]


def is_recoverable_tenant_label(value: str) -> bool:
    normalized = value.strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if len(compact) < 4 or re.fullmatch(
        r"(?:my)?(?:agency|studio|studios|media|creative)[a-z]?\d*",
        compact,
    ):
        return False
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
    meaningful = []
    for token in tokens:
        alpha = re.sub(r"\d", "", token)
        if len(alpha) >= 4 and alpha not in GENERIC_TENANT_TOKENS:
            meaningful.append(alpha)
    return bool(meaningful)


def terminal_platform_observation(
    candidate: DiscoveredHost,
    *,
    status_code: int | None = None,
    final_url: str | None = None,
    error: str = "",
    fetch_attempts: int = 0,
) -> SiteObservation | None:
    if not is_platform_tenant(candidate):
        return None
    tenant_label = platform_tenant_label(candidate)
    common = {
        "hostname": candidate.hostname,
        "live": False,
        "final_url": final_url,
        "canonical_host": candidate.hostname if final_url else None,
        "status_code": status_code,
        "fetch_attempts": fetch_attempts,
        "canonical_recovery_required": bool(tenant_label),
        "tenant_label": tenant_label,
    }
    if status_code in PLATFORM_BLOCKED_STATUS_CODES:
        return SiteObservation(
            **common,
            fetch_outcome="platform_blocked",
            fetch_disposition=(
                f"Shared platform endpoint returned HTTP {status_code}; "
                "canonical company domain recovery is required"
            ),
        )
    if status_code in NOT_PUBLIC_PLATFORM_STATUS_CODES:
        return SiteObservation(
            **common,
            fetch_outcome="not_public_storefront",
            fetch_disposition=(
                f"Shared platform endpoint returned HTTP {status_code}; "
                "it is not a public company storefront"
            ),
        )
    normalized_error = error.lower()
    if (
        "dns resolution failed" in normalized_error
        or "dns resolution returned no" in normalized_error
    ):
        return SiteObservation(
            **common,
            fetch_outcome="retired_hostname",
            fetch_disposition=(
                "Historical platform hostname no longer resolves; "
                "canonical company domain recovery is required"
            ),
        )
    if (
        "certificate_verify_failed" in normalized_error
        or "certificate verify failed" in normalized_error
    ):
        return SiteObservation(
            **common,
            fetch_outcome="not_public_storefront",
            fetch_disposition=(
                "Direct platform hostname failed TLS certificate validation; "
                "it is not a safe public company storefront"
            ),
        )
    if any(
        marker in normalized_error
        for marker in (
            "sslv3_alert_handshake_failure",
            "tlsv1_alert_handshake_failure",
            "ssl/tls alert handshake failure",
            "tls handshake failure",
        )
    ):
        return SiteObservation(
            **common,
            fetch_outcome="not_public_storefront",
            fetch_disposition=(
                "Direct platform hostname rejected an ordinary TLS handshake; "
                "it is not a public company storefront"
            ),
        )
    if "refusing non-public address" in normalized_error or (
        "refusing non-public connected peer" in normalized_error
    ):
        return SiteObservation(
            **common,
            fetch_outcome="not_public_storefront",
            fetch_disposition=(
                "Direct platform hostname resolved to a non-public address; "
                "the request was refused by network safety policy"
            ),
        )
    return None


def platform_failure_signature(
    candidate: DiscoveredHost,
    observation: SiteObservation,
) -> str:
    """Return a stable circuit signature for a direct platform-host failure."""
    if not is_platform_tenant(candidate) or observation.live:
        return ""
    status_code = observation.status_code
    if status_code is not None and status_code >= 400:
        return f"http_{status_code}"

    detail = " ".join(
        value
        for value in (observation.error or "", observation.fetch_disposition)
        if value
    ).lower()
    if "certificate_verify_failed" in detail or "certificate verify failed" in detail or (
        "failed tls certificate validation" in detail
    ):
        return "tls_invalid"
    if any(
        marker in detail
        for marker in (
            "sslv3_alert_handshake_failure",
            "tlsv1_alert_handshake_failure",
            "ssl/tls alert handshake failure",
            "tls handshake failure",
            "rejected an ordinary tls handshake",
        )
    ):
        return "tls_handshake_rejected"
    if "refusing non-public address" in detail or "resolved to a non-public address" in detail or (
        "refusing non-public connected peer" in detail
    ):
        return "private_address"
    if "dns resolution failed" in detail or "dns resolution returned no" in detail or (
        observation.fetch_outcome == "retired_hostname"
    ):
        return "dns_unresolved"
    if "connecttimeout" in detail:
        return "connect_timeout"
    if "readtimeout" in detail:
        return "read_timeout"
    if "writetimeout" in detail:
        return "write_timeout"
    if "pooltimeout" in detail or "timeouterror" in detail:
        return "transport_timeout"
    if "connecterror" in detail:
        return "connect_error"
    if any(
        token in detail
        for token in (
            "readerror",
            "writeerror",
            "networkerror",
            "remoteprotocolerror",
            "localprotocolerror",
        )
    ):
        return "transport_error"
    if observation.fetch_outcome in {
        "platform_blocked",
        "not_public_storefront",
    }:
        return observation.fetch_outcome
    if observation.error or observation.fetch_outcome == "retryable_error":
        return "platform_error"
    return ""


def is_terminal_fetch_outcome(observation: SiteObservation) -> bool:
    return observation.fetch_outcome in TERMINAL_FETCH_OUTCOMES


@dataclass(frozen=True)
class OpenedApexCircuit:
    apex: str
    outcome_signature: str
    threshold: int
    opened_at: str
    cooldown_until: str
    sample_detail: str


@dataclass(frozen=True)
class ApexFailureWindow:
    apex: str
    failure_count: int
    signature_counts: dict[str, int]
    sample_detail: str
    updated_at: str


@dataclass(frozen=True)
class ApexFailureEvent:
    apex: str
    signature: str
    attempt_count: int
    sample_detail: str
    observed_at: str
    threshold: int
    cooldown_hours: int


@dataclass(frozen=True)
class ApexFailureRecord:
    window: ApexFailureWindow
    circuit: OpenedApexCircuit | None = None
    opened: bool = False


class PlatformApexCircuitBreaker:
    def __init__(
        self,
        *,
        active_cooldowns: Mapping[str, Mapping[str, object]] | None = None,
        active_failure_windows: Mapping[str, Mapping[str, object]] | None = None,
        threshold: int = APEX_CIRCUIT_THRESHOLD,
        cooldown_hours: int = APEX_CIRCUIT_COOLDOWN_HOURS,
        on_open: Callable[[OpenedApexCircuit], None] | None = None,
        on_window_update: Callable[[ApexFailureWindow], None] | None = None,
        on_window_reset: Callable[[str], None] | None = None,
        on_failure_record: Callable[[ApexFailureEvent], ApexFailureRecord] | None = None,
        active_cooldown_lookup: (
            Callable[[str], Mapping[str, object] | None] | None
        ) = None,
        on_persistence_error: Callable[[str, Exception], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_hours = max(1, int(cooldown_hours))
        self.on_open = on_open
        self.on_window_update = on_window_update
        self.on_window_reset = on_window_reset
        self.on_failure_record = on_failure_record
        self.active_cooldown_lookup = active_cooldown_lookup
        self.on_persistence_error = on_persistence_error
        self._now = now or (lambda: datetime.now(UTC))
        self._active: dict[str, dict[str, object]] = {
            str(apex).strip().lower().rstrip("."): dict(value)
            for apex, value in (active_cooldowns or {}).items()
        }
        self._windows: dict[str, ApexFailureWindow] = {}
        for raw_apex, raw_window in (active_failure_windows or {}).items():
            apex = self._normalize_apex(raw_apex)
            if not apex:
                continue
            try:
                failure_count = max(1, int(raw_window.get("failure_count") or 0))
            except (TypeError, ValueError):
                continue
            raw_counts = raw_window.get("signature_counts")
            if not isinstance(raw_counts, Mapping):
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
            if not signature_counts:
                continue
            self._windows[apex] = ApexFailureWindow(
                apex=apex,
                failure_count=failure_count,
                signature_counts=signature_counts,
                sample_detail=str(raw_window.get("sample_detail") or "")[:500],
                updated_at=str(raw_window.get("updated_at") or ""),
            )
        self.opened_apexes: set[str] = set()
        self.skipped_by_apex: dict[str, int] = {}
        self.persistence_errors: list[str] = []

    @staticmethod
    def _normalize_apex(value: object) -> str:
        return str(value).strip().lower().rstrip(".")

    def _handle_persistence_error(self, operation: str, exc: Exception) -> bool:
        detail = f"{operation}: {type(exc).__name__}: {exc}"[:500]
        self.persistence_errors.append(detail)
        self.persistence_errors = self.persistence_errors[-20:]
        if self.on_persistence_error is None:
            return False
        self.on_persistence_error(operation, exc)
        return True

    def skip_observation(self, candidate: DiscoveredHost) -> SiteObservation | None:
        if not is_platform_tenant(candidate):
            return None
        apex = self._normalize_apex(candidate.source_apex)
        if self.active_cooldown_lookup is not None:
            try:
                current = self.active_cooldown_lookup(apex)
            except Exception as exc:
                if not self._handle_persistence_error("cooldown_lookup", exc):
                    raise
                current = None
            if current is not None:
                self._active[apex] = dict(current)
        cooldown = self._active.get(apex)
        if cooldown is None:
            return None
        cooldown_until = str(cooldown.get("cooldown_until") or "")
        try:
            expiry = datetime.fromisoformat(cooldown_until)
        except ValueError:
            self._active.pop(apex, None)
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= self._now():
            self._active.pop(apex, None)
            return None
        self.skipped_by_apex[apex] = self.skipped_by_apex.get(apex, 0) + 1
        signature = str(cooldown.get("outcome_signature") or "blocked")
        tenant_label = platform_tenant_label(candidate)
        return SiteObservation(
            hostname=candidate.hostname,
            fetch_outcome="circuit_open",
            fetch_disposition=(
                f"Skipped because {apex} circuit is open after {signature}; "
                f"retry after {cooldown_until}"
            ),
            canonical_recovery_required=bool(tenant_label),
            tenant_label=tenant_label,
            live=False,
        )

    def record(self, candidate: DiscoveredHost, observation: SiteObservation) -> None:
        if not is_platform_tenant(candidate):
            return
        apex = self._normalize_apex(candidate.source_apex)
        signature = platform_failure_signature(candidate, observation)
        if not signature:
            healthy = observation.live or (
                not observation.error
                and observation.status_code is not None
                and observation.status_code < 400
            )
            if healthy:
                self._windows.pop(apex, None)
                if self.on_window_reset is not None:
                    try:
                        self.on_window_reset(apex)
                    except Exception as exc:
                        if not self._handle_persistence_error("window_reset", exc):
                            raise
            return
        previous = self._windows.get(apex)
        signatures = Counter(previous.signature_counts if previous is not None else {})
        attempt_count = max(1, int(observation.fetch_attempts or 0))
        signatures[signature] += attempt_count
        count = (previous.failure_count if previous is not None else 0) + attempt_count
        sample_detail = observation.fetch_disposition or observation.error or signature
        observed_at = self._now()
        if self.on_failure_record is not None:
            try:
                result = self.on_failure_record(
                    ApexFailureEvent(
                        apex=apex,
                        signature=signature,
                        attempt_count=attempt_count,
                        sample_detail=sample_detail[:500],
                        observed_at=observed_at.isoformat(),
                        threshold=self.threshold,
                        cooldown_hours=self.cooldown_hours,
                    )
                )
            except Exception as exc:
                if not self._handle_persistence_error("failure_record", exc):
                    raise
            else:
                self._windows[apex] = result.window
                if result.circuit is not None:
                    self._active[apex] = {
                        "outcome_signature": result.circuit.outcome_signature,
                        "threshold": result.circuit.threshold,
                        "opened_at": result.circuit.opened_at,
                        "cooldown_until": result.circuit.cooldown_until,
                        "sample_detail": result.circuit.sample_detail,
                    }
                if result.opened and result.circuit is not None:
                    self.opened_apexes.add(apex)
                    if self.on_open is not None:
                        try:
                            self.on_open(result.circuit)
                        except Exception as exc:
                            if not self._handle_persistence_error("circuit_open", exc):
                                raise
                return
        window = ApexFailureWindow(
            apex=apex,
            failure_count=count,
            signature_counts=dict(signatures),
            sample_detail=sample_detail[:500],
            updated_at=observed_at.isoformat(),
        )
        self._windows[apex] = window
        if self.on_window_update is not None:
            try:
                self.on_window_update(window)
            except Exception as exc:
                if not self._handle_persistence_error("window_update", exc):
                    raise
        if count < self.threshold or apex in self._active:
            return
        opened = self._now()
        cooldown_until = opened + timedelta(hours=self.cooldown_hours)
        outcome_signature = (
            signature if len(signatures) == 1 else "mixed_platform_failures"
        )
        signature_breakdown = ", ".join(
            f"{name}={signature_count}"
            for name, signature_count in sorted(
                signatures.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        if len(signatures) > 1:
            sample_detail = f"{sample_detail}; consecutive signatures: {signature_breakdown}"
        record = OpenedApexCircuit(
            apex=apex,
            outcome_signature=outcome_signature,
            threshold=self.threshold,
            opened_at=opened.isoformat(),
            cooldown_until=cooldown_until.isoformat(),
            sample_detail=sample_detail,
        )
        self._active[apex] = {
            "outcome_signature": record.outcome_signature,
            "threshold": record.threshold,
            "opened_at": record.opened_at,
            "cooldown_until": record.cooldown_until,
            "sample_detail": record.sample_detail,
        }
        self.opened_apexes.add(apex)
        if self.on_open is not None:
            try:
                self.on_open(record)
            except Exception as exc:
                if not self._handle_persistence_error("circuit_open", exc):
                    raise

    def summary(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "cooldown_hours": self.cooldown_hours,
            "opened_apexes": sorted(self.opened_apexes),
            "active_apexes": sorted(self._active),
            "skipped": sum(self.skipped_by_apex.values()),
            "skipped_by_apex": dict(sorted(self.skipped_by_apex.items())),
            "failure_windows": {
                apex: {
                    "failure_count": window.failure_count,
                    "signature_counts": dict(sorted(window.signature_counts.items())),
                    "updated_at": window.updated_at,
                }
                for apex, window in sorted(self._windows.items())
            },
            "persistence_error_count": len(self.persistence_errors),
            "persistence_errors": list(self.persistence_errors),
        }
