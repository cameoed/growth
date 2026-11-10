"""Allocate directory HTML work using observed, distinct source-company yield.

This is an intentional collection policy, not evidence pruning or proof of exhaustion.
The caller must publish the current page before yielding and keep continuation due.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

POLICY_REVISION = "directory-yield-v1"
MIN_FRESH_PAGES = 20
EMPTY_STREAK = 12
LOW_YIELD_MIN_PAGES = 40
LOW_YIELD_WINDOW = 20
LOW_YIELD_MAX_COMPANIES = 5
RECENT_YIELD_WINDOW = 5
RECENT_YIELD_MAX_COMPANIES = 1


@dataclass(slots=True)
class CrawlYieldMonitor:
    seen_hosts: set[str] = field(default_factory=set)
    fresh_page_yields: list[int] = field(default_factory=list)
    consecutive_empty: int = 0
    cached_new_relationships: int = 0
    last_cached_discovery_at: int = 0

    def observe(self, hosts: Iterable[str], *, fresh: bool) -> None:
        hosts = set(hosts)
        new_hosts = hosts - self.seen_hosts
        self.seen_hosts.update(hosts)
        if not fresh:
            if count := len(new_hosts):
                # A cached page may contain companies omitted by an earlier result cap.
                # Real progress from replay must protect that branch from a stale dry spell.
                self.cached_new_relationships += count
                self.consecutive_empty = 0
                self.last_cached_discovery_at = len(self.fresh_page_yields)
            return
        count = len(new_hosts)
        self.fresh_page_yields.append(count)
        self.consecutive_empty = 0 if count else self.consecutive_empty + 1

    @property
    def stop_reason(self) -> str | None:
        pages = len(self.fresh_page_yields)
        if pages >= MIN_FRESH_PAGES and self.consecutive_empty >= EMPTY_STREAK:
            return "consecutive_empty_pages"
        if (
            pages >= LOW_YIELD_MIN_PAGES
            and pages - self.last_cached_discovery_at >= LOW_YIELD_WINDOW
            and sum(self.fresh_page_yields[-LOW_YIELD_WINDOW:]) <= LOW_YIELD_MAX_COMPANIES
            and sum(self.fresh_page_yields[-RECENT_YIELD_WINDOW:]) <= RECENT_YIELD_MAX_COMPANIES
        ):
            return "low_rolling_yield"
        return None

    def summary(self) -> dict:
        return {
            "policy": POLICY_REVISION,
            "basis": "distinct_new_source_relationships",
            "fresh_page_yields": list(self.fresh_page_yields),
            "consecutive_empty": self.consecutive_empty,
            "new_relationships": sum(self.fresh_page_yields),
            "cached_new_relationships": self.cached_new_relationships,
        }
