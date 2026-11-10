"""Local paid-work plans; reads never call a provider or extend evidence freshness."""

import json
from collections import OrderedDict
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from threading import Lock

from .ai import AI_PROMPT_VERSION, analysis_cache_keys
from .dataforseo_intelligence import (
    SCANNER_ID,
    SCANNER_VERSION,
    group_seo_cohort,
    seo_registered_domain,
)
from .models import AIAnalysis, DiscoveredHost, ScoreCard, SiteObservation
from .rehydration import REHYDRATION_DAYS

PAID_REUSE_DAYS = REHYDRATION_DAYS
_AI_INPUT_MEMO = OrderedDict()
_AI_INPUT_LOCK = Lock()
_AI_INPUT_LIMIT = 1024


def _memoized_input(version, key, value=None):
    if version is None:
        return value
    key = (version, key)
    with _AI_INPUT_LOCK:
        if value is not None:
            _AI_INPUT_MEMO[key] = value
            if len(_AI_INPUT_MEMO) > _AI_INPUT_LIMIT:
                _AI_INPUT_MEMO.popitem(last=False)
        if key in _AI_INPUT_MEMO:
            _AI_INPUT_MEMO.move_to_end(key)
            return _AI_INPUT_MEMO[key]
        return value


def record_ai_review(store, content_key, hostname, model):
    store.connection.execute(
        "INSERT OR REPLACE INTO ai_review_receipts VALUES (?,?,?,?,?)",
        (content_key, hostname, model, AI_PROMPT_VERSION, datetime.now(UTC).isoformat()),
    )
    store.connection.commit()


def has_recent_ai_review(store, candidate, *, model, now=None, keys=(), input_version=None):
    """A paid-review hold is separate from applying an exact-input cached analysis."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=PAID_REUSE_DAYS)).isoformat()
    stamp = now.isoformat()
    row = store.connection.execute(
        "SELECT 1 FROM ai_review_receipts WHERE hostname=? AND model=? AND prompt_version=? "
        "AND completed_at>? AND completed_at<=? LIMIT 1",
        (candidate.hostname, model, AI_PROMPT_VERSION, cutoff, stamp),
    ).fetchone()
    if row:
        return True

    def completed(key):
        saved = store.connection.execute(
            "SELECT analysis_json FROM ai_cache WHERE content_key=? AND model=? "
            "AND created_at>? AND created_at<=?", (key, model, cutoff, stamp),
        ).fetchone()
        if not saved:
            return False
        try:
            AIAnalysis.model_validate_json(saved["analysis_json"])
            return True
        except ValueError:
            return False

    if any(completed(key) for key in keys):
        return True
    # Link legacy results only when the historical provider-input key matches.
    # Reused snapshot dates never extend the actual cache completion clock.
    rows = store.connection.execute(
        "SELECT s.id FROM snapshots s "
        "JOIN candidates c ON c.id=s.candidate_id WHERE c.hostname=? AND s.ai_json IS NOT NULL "
        "AND s.observed_at>? AND s.observed_at<=? ORDER BY s.id DESC",
        (candidate.hostname, cutoff, stamp),
    )
    for row in rows:
        try:
            memo_key = (AI_PROMPT_VERSION, model, row["id"])
            fingerprints = _memoized_input(input_version, memo_key)
            if fingerprints is None:
                raw = store.connection.execute(
                    "SELECT observation_json,score_json FROM snapshots WHERE id=?", (row["id"],),
                ).fetchone()
                if raw is None:
                    continue
                observation = SiteObservation.model_validate_json(raw["observation_json"])
                score = ScoreCard.model_validate_json(raw["score_json"])
                fingerprints = _memoized_input(
                    input_version, memo_key,
                    analysis_cache_keys(candidate, observation, score, model=model),
                )
            for key in fingerprints:
                if completed(key):
                    return True
        except ValueError:
            continue
    return False


def seo_stage_receipts(store, domains=None, *, now=None):
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=PAID_REUSE_DAYS)).isoformat()
    stamp = now.isoformat()
    roots = (
        list(dict.fromkeys(filter(None, (seo_registered_domain(d) for d in domains))))
        if domains is not None else None
    )
    batches = (None,) if roots is None else (roots[i:i + 500] for i in range(0, len(roots), 500))
    receipts = {}
    for batch in batches:
        condition = (
            "AND seo_registered_domain(company_key) IN (" + ",".join("?" for _ in batch) + ")"
            if batch else ""
        )
        rows = store.connection.execute(
            "SELECT seo_registered_domain(company_key) AS root,scope_key "
            "FROM scanner_executions WHERE scanner_id=? AND scanner_version=? "
            "AND status IN ('success','partial') "
            "AND finished_at IS NOT NULL AND observation_count>0 "
            "AND recorded_at<=? AND started_at<=? AND finished_at<=? AND finished_at>? "
            + condition,
            (SCANNER_ID, SCANNER_VERSION, stamp, stamp, stamp, cutoff, *(batch or ())),
        )
        for row in rows:
            scope = row["scope_key"]
            if not scope.startswith("market_source:dataforseo:"):
                continue
            stage = scope.rsplit(":", 1)[-1]
            if row["root"] and stage in {"bulk", "domain_overview"}:
                receipts[(row["root"], stage)] = True
    return receipts


def prior_detail_scope(store, cohort, site_limit, *, now=None):
    now = now or datetime.now(UTC)
    roots = {row["seo_domain"] for row in cohort}
    rows = store.connection.execute(
        "SELECT state_json FROM smart_scan_runs WHERE started_at>? AND started_at<=? "
        "AND json_extract(state_json,'$.seo_site_limit')=? ORDER BY updated_at DESC",
        ((now - timedelta(days=PAID_REUSE_DAYS)).isoformat(), now.isoformat(), site_limit),
    )
    for row in rows:
        state = json.loads(row["state_json"])
        old = group_seo_cohort(state.get("_dataforseo_cohort") or [])
        detail = group_seo_cohort(state.get("_dataforseo_detail_cohort") or [])
        if detail and {item["seo_domain"] for item in old} == roots:
            return [item["seo_domain"] for item in detail if item["seo_domain"] in roots]
    return None


def seo_plan(store, site_limit, detail_limit, *, now=None):
    from .sales_engine.summaries import _snapshot

    with _snapshot(store.connection):
        return _seo_plan(store, site_limit, detail_limit, now=now)


def _seo_plan(store, site_limit, detail_limit, *, now=None):
    now = now or datetime.now(UTC)
    receipts = seo_stage_receipts(store, now=now)
    bulk_fresh = {root for root, stage in receipts if stage == "bulk"}
    detail_fresh = {root for root, stage in receipts if stage == "domain_overview"}
    bulk = group_seo_cohort(store.dataforseo_ranking_candidates(
        limit=site_limit, as_of=now, exclude_roots=bulk_fresh, bulk_selection=True,
    ))
    bulk_detail_due = sum(item["seo_domain"] not in detail_fresh for item in bulk)
    spare = min(site_limit - len(bulk), detail_limit - bulk_detail_due)
    detail_only = group_seo_cohort(store.dataforseo_ranking_candidates(
        limit=spare, as_of=now, only_roots=bulk_fresh - detail_fresh, bulk_selection=True,
    )) if spare > 0 and bulk_fresh - detail_fresh else []
    cohort = group_seo_cohort([*bulk, *detail_only])
    for item in cohort:
        root = item["seo_domain"]
        item["seo_due"] = root not in bulk_fresh
        item["seo_detail_due"] = root not in detail_fresh
    return {"cohort": cohort, "bulk_due": len(bulk), "bulk_reused": len(detail_only),
            "detail_scope": None if bulk else [item["seo_domain"] for item in detail_only],
            "detail_due": min(detail_limit, bulk_detail_due + len(detail_only)),
            "reuse_days": PAID_REUSE_DAYS}


def _ai_items(store, hosts):
    from .cli import InspectedCandidate, _eligible_for_ai_review

    if not hosts:
        return
    snapshots = store.latest_snapshots(hosts, ai_review_only=True)
    marks = ",".join("?" for _ in hosts)
    candidates = {
        row["hostname"]: DiscoveredHost(
            hostname=row["hostname"], source_apex=row["source_apex"],
            discovery_rank=row["discovery_rank"] or 0,
        )
        for row in store.connection.execute(
            "SELECT hostname,source_apex,discovery_rank FROM candidates "
            f"WHERE hostname IN ({marks})", hosts,
        )
    }
    for host in hosts:
        saved = snapshots.get(host)
        if saved is None or host not in candidates:
            continue
        item = InspectedCandidate(candidates[host], saved["observation"], saved["score"])
        if _eligible_for_ai_review(item, 45):
            yield item


def ai_plan(store, site_limit, *, model, web_limit):
    from .sales_engine.list_cache import flight
    from .sales_engine.summaries import _snapshot

    scope = store.opened_read_only_identity if not store.connection.in_transaction else None
    # Keep two overlapping quote changes from loading the same evidence in parallel.
    coalesce = flight(("ai-review", scope)) if scope is not None else nullcontext()
    with coalesce, _snapshot(store.connection):
        return _ai_plan(store, site_limit, model=model, web_limit=web_limit, scope=scope)


def _has_ai_cache(store, key):
    row = store.connection.execute(
        "SELECT analysis_json FROM ai_cache WHERE content_key=?", (key,),
    ).fetchone()
    if row is None:
        return False
    try:
        AIAnalysis.model_validate_json(row["analysis_json"])
    except ValueError:
        return False
    return True


def _ai_input_versions(store, hosts, scope):
    if scope is None or not store.connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='engine_mission_members'"
    ).fetchone():
        return {}
    # Candidate and snapshot writes, including historical edits and canonical moves,
    # increment the membership revision transactionally. Store hashes and scalar
    # priorities only, and evaluate dates and cache availability on every quote.
    versions = {}
    for start in range(0, len(hosts), 250):
        batch = hosts[start:start + 250]
        marks = ",".join("?" for _ in batch)
        rows = store.connection.execute(
            "SELECT c.hostname,m.candidate_id,m.snapshot_id,m.revision "
            "FROM candidates c JOIN engine_mission_members m ON m.candidate_id=c.id "
            f"WHERE c.hostname IN ({marks}) AND m.snapshot_id="
            "(SELECT MAX(id) FROM snapshots s WHERE s.candidate_id=c.id)", batch,
        )
        versions.update((row["hostname"], (scope, *tuple(row)[1:])) for row in rows)
    return versions


def _ai_plan(store, site_limit, *, model, web_limit, scope=None):
    # Read full evidence only for recovery exceptions and the selected end of the
    # shortlist. Keep large decoded observations out of the ordering pass.
    batch_size = 8
    recoveries = [str(item["hostname"]) for item in store.platform_identity_recovery_candidates(
        limit=None,
    )]
    targets = store.ai_review_target_hosts(min_score=45)
    hosts = list(dict.fromkeys([*recoveries, *targets]))
    versions = _ai_input_versions(store, hosts, scope)
    priorities = {
        host: priority for host, version in versions.items()
        if (priority := _memoized_input(version, "priority")) is not None
    }
    recovery_set = set(recoveries)
    ordered_targets = store.ai_review_target_priorities(
        [host for host in targets if host not in recovery_set and host not in priorities],
        min_score=45,
    )
    for host, priority in ordered_targets.items():
        priorities[host] = _memoized_input(versions.get(host), "priority", priority)
    # Recovery candidates may be eligible through a different rule, including when
    # their website is unavailable. Retain full validation for those and legacy rows.
    remaining = [host for host in hosts if host not in priorities]
    for start in range(0, len(remaining), batch_size):
        for item in _ai_items(store, remaining[start:start + batch_size]):
            host = item.candidate.hostname
            priority = max(
                item.score.placement.score, item.score.contact_priority, item.score.total,
                item.score.creative_capacity.total, round(item.candidate.discovery_rank * 2),
            )
            priorities[host] = _memoized_input(versions.get(host), "priority", priority)
    hosts = sorted((host for host in hosts if host in priorities),
                   key=priorities.__getitem__, reverse=True)

    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=PAID_REUSE_DAYS)).isoformat()
    stamp = now.isoformat()
    cache_exists, has_legacy_reuse = store.connection.execute(
        "SELECT EXISTS(SELECT 1 FROM ai_cache), EXISTS(SELECT 1 FROM ai_cache "
        "WHERE model=? AND created_at>? AND created_at<=?)", (model, cutoff, stamp),
    ).fetchone()
    selected, reused, web_due = [], 0, 0
    current_key = (AI_PROMPT_VERSION, model, "current")
    for start in range(0, len(hosts), batch_size):
        batch = hosts[start:start + batch_size]
        marks = ",".join("?" for _ in batch)
        held = {row[0] for row in store.connection.execute(
            f"SELECT hostname FROM ai_review_receipts WHERE hostname IN ({marks}) "
            "AND model=? AND prompt_version=? AND completed_at>? AND completed_at<=?",
            [*batch, model, AI_PROMPT_VERSION, cutoff, stamp],
        )}
        inputs = {}
        if cache_exists:
            inputs = {
                host: cached for host in batch if host not in held
                if (cached := _memoized_input(versions.get(host), current_key)) is not None
            }
            missing = [host for host in batch if host not in held and host not in inputs]
            for item in _ai_items(store, missing):
                host = item.candidate.hostname
                inputs[host] = _memoized_input(versions.get(host), current_key, (
                    item.candidate.source_apex,
                    *analysis_cache_keys(item.candidate, item.observation, item.score, model=model),
                ))
        for host in batch:
            if host in held:
                reused += 1
                continue
            if cache_exists:
                if host not in inputs:
                    continue
                source_apex, web_key, standard_key = inputs[host]
                if (_has_ai_cache(store, web_key)
                        or (web_due >= web_limit and _has_ai_cache(store, standard_key))
                        or (has_legacy_reuse and has_recent_ai_review(
                            store, DiscoveredHost(hostname=host, source_apex=source_apex),
                            model=model, now=now, input_version=versions.get(host),
                            keys=(web_key, standard_key),
                        ))):
                    reused += 1
                    continue
            selected.append(host)
            web_due += int(web_due < web_limit)
            if len(selected) >= site_limit:
                break
        if len(selected) >= site_limit:
            break
    return {"hosts": selected, "selected": len(selected), "due": len(selected),
            "reused": reused, "web_due": web_due, "reuse_days": PAID_REUSE_DAYS}
