"""Small visible-company reads, with revision- and time-bounded process caching.

No scorer, historical observation, workload or provider is consulted here. The
canonical ranking remains the only source of a displayed score. Full source
packets retain their existing API and are loaded when the operator requests them.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import RLock

from ..email_lookup import email_lookup_estimate
from ..naming import concise_company_name
from ..store import COMPANY_SCAN_EFFORT
from ..treg_people import SCANNER_ID as PEOPLE_SCANNER_ID
from ..treg_people import resolve_treg_credentials
from .contacts import COMPANY_CONTACT_DISPLAY_LIMIT, FRESH_DAYS, recommend_contacts
from .ranking import policy_version
from .ranking_modes import (
    COLUMNS,
    missing_lens_count,
    projection_from,
    score_version,
    validate_mode,
)
from .ranking_modes import VERSION as LENS_VERSION
from .score_breakdown import beings_breakdown

MAX_ENTRIES = 128
TTL_SECONDS = 60
_CACHE = OrderedDict()
_LOCK = RLock()
_PROJECTION_COLUMNS = (
    "company_key,candidate_id,snapshot_id,company_name,score,policy_version,"
    "evidence_version,as_of,valid_until,dirty"
)
_ACCOUNT_COLUMNS = "id,company,website,website_host,lifecycle,updated_at"


def _columns(mode):
    validate_mode(mode)
    columns = [f"r.{name}" for name in _PROJECTION_COLUMNS.split(",")]
    if mode != "balanced":
        columns[columns.index("r.score")] = f"l.{COLUMNS[mode]} AS score"
        columns.append("l.version AS lens_version")
    return ",".join(columns)


def _now(value=None):
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("summary reference time requires a timezone")
    return value.astimezone(UTC)


def _date(value):
    try:
        value = datetime.fromisoformat(value)
        return value.astimezone(UTC) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def _list_deadline(now):
    # Stable within the short display window, so unchanged list responses can be 304.
    return datetime.fromtimestamp((int(now.timestamp()) // TTL_SECONDS + 1) * TTL_SECONDS, UTC)


@contextmanager
def _snapshot(connection):
    # Read one consistent revision without acquiring the SQLite writer lock.
    # Caller-owned/uncommitted data must never enter a shared process cache.
    own = not connection.in_transaction
    if own:
        connection.execute("BEGIN")
    try:
        yield own
    finally:
        if own:
            connection.rollback()


def _cache_key(store, selection):
    stat = store.path.stat()
    return (str(store.path.resolve()), stat.st_dev, stat.st_ino, selection)


def _get(key, revision, now):
    if key is None:
        return None
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        saved_revision, start, end, encoded, etag = entry
        if saved_revision != revision or not start <= now < end:
            del _CACHE[key]
            return None
        _CACHE.move_to_end(key)
        return json.loads(encoded), etag


def _put(key, revision, now, expires, payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha256((revision + encoded).encode()).hexdigest() + '"'
    if key is not None:
        with _LOCK:
            _CACHE[key] = (revision, now, expires, encoded, etag)
            _CACHE.move_to_end(key)
            while len(_CACHE) > MAX_ENTRIES:
                _CACHE.popitem(last=False)
    return payload, etag


def _identity(connection, candidate_id):
    row = connection.execute(
        "SELECT c.id,c.hostname,c.label_hint,s.id AS snapshot_id,s.observed_at,"
        "COALESCE(ti.canonical_domain,c.hostname) AS company_key FROM candidates c "
        "LEFT JOIN snapshots s ON s.id=(SELECT id FROM snapshots "
        "WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1) "
        "LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id WHERE c.id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Company not found")
    return dict(row)


def _current(row, now, mode="balanced"):
    return bool(
        row
        and row["policy_version"] == policy_version()
        and not row["dirty"]
        and row["as_of"] <= now.isoformat() < row["valid_until"]
        and (mode == "balanced" or row.get("lens_version") == LENS_VERSION)
    )


def _scan_dates(connection, key, identity, now):
    stamp = now.isoformat()
    website = connection.execute(
        "SELECT completed_at FROM company_scan_work WHERE company_key=? AND effort_key=? "
        "AND completed_at<=? AND completed_snapshot_id IS NOT NULL",
        (key, COMPANY_SCAN_EFFORT, stamp),
    ).fetchone()
    people = connection.execute(
        "SELECT MAX(COALESCE(finished_at,started_at)) FROM scanner_executions "
        "WHERE company_key=? AND scanner_id=? AND status IN ('success','success_empty') "
        "AND recorded_at<=? AND started_at<=? AND COALESCE(finished_at,started_at)<=?",
        (key, PEOPLE_SCANNER_ID, stamp, stamp, stamp),
    ).fetchone()
    contacts = connection.execute(
        "SELECT MAX(a.first_observed_at) FROM atomic_observations a "
        "JOIN scanner_executions e ON e.id=a.execution_id "
        "WHERE a.company_key=? AND a.scanner_id='microlink_hydration_v1' "
        "AND a.predicate='microlink.page' AND json_extract(a.metadata_json,'$.feature')='contacts' "
        "AND a.first_observed_at<=? AND e.finished_at<=? AND e.recorded_at<=?",
        (key, stamp, stamp, stamp),
    ).fetchone()
    observed = identity.get("observed_at") if identity else None
    website_stamp = website[0] if website else None
    if observed and observed <= stamp and (not website_stamp or observed > website_stamp):
        website_stamp = observed
    return {
        "website_checked_at": website_stamp,
        "contact_crawled_at": contacts[0],
        "people_checked_at": people[0],
    }


def _account(connection, *, account_id=None, company_key=None):
    predicate, value = (
        ("id=?", account_id) if account_id is not None else ("website_host=?", company_key)
    )
    row = connection.execute(
        f"SELECT {_ACCOUNT_COLUMNS} FROM sales_accounts WHERE {predicate} ORDER BY id DESC LIMIT 1",
        (value,),
    ).fetchone()
    if account_id is not None and row is None:
        raise LookupError("Company record not found")
    return dict(row) if row else None


def _contact_expiry(connection, key, public, licensed, observed, now, *, horizon=None):
    boundaries = [horizon or now + timedelta(seconds=TTL_SECONDS)]

    def add(value, *, age=False):
        stamp = _date(value)
        if stamp is None:
            return
        if stamp > now:
            boundaries.append(stamp)
        if age and stamp + timedelta(days=FRESH_DAYS, microseconds=1) > now:
            boundaries.append(stamp + timedelta(days=FRESH_DAYS, microseconds=1))

    add(observed, age=True)
    add(licensed.get("scanned_at"), age=True)
    add(licensed.get("expires_at"))
    for contacts, field in [(public, "people"), (public, "emails"), (public, "phones"),
                            (licensed, "people")]:
        rows = contacts.get(field) or []
        for row in rows if horizon is not None else rows[:100]:
            if isinstance(row, dict):
                add(row.get("employment_verified_at") or row.get("observed_at"), age=True)
                add(row.get("expires_at"))
    # Email overlays and future provider receipts may cross a boundary without
    # another write. Read only this company's indexed receipt timestamps.
    for row in connection.execute(
        "SELECT observed_at,expires_at FROM company_email_lookups WHERE company_key=? "
        "AND (observed_at>? OR expires_at>?)",
        (key, now.isoformat(), now.isoformat()),
    ):
        for value in row:
            add(value)
    for row in connection.execute(
        "SELECT first_observed_at,expires_at FROM atomic_observations "
        "WHERE company_key=? AND scanner_id=? "
        "AND predicate='contact.provider_professional_person' "
        "AND (first_observed_at>? OR expires_at>?)",
        (key, PEOPLE_SCANNER_ID, now.isoformat(), now.isoformat()),
    ):
        for value in row:
            add(value)
    for row in connection.execute(
        "SELECT first_observed_at,expires_at FROM atomic_observations "
        "WHERE company_key=? AND scanner_id='microlink_hydration_v1' "
        "AND predicate='microlink.page' AND (first_observed_at>? OR expires_at>?)",
        (key, now.isoformat(), now.isoformat()),
    ):
        for value in row:
            add(value)
    for row in connection.execute(
        "SELECT started_at,finished_at,recorded_at FROM scanner_executions "
        "WHERE company_key=? AND scanner_id IN (?,'microlink_hydration_v1') "
        "AND (started_at>? OR finished_at>? OR recorded_at>?)",
        (key, PEOPLE_SCANNER_ID, *([now.isoformat()] * 3)),
    ):
        for value in row:
            add(value)
    return min(boundaries)


def company_summary(engine, candidate_id=None, *, account_id=None, reference_time=None,
                    mode="balanced", _licensed=None):
    """Return (visible payload, ETag), validating skinny revisions before cache reuse."""
    now = _now(reference_time)
    validate_mode(mode)
    connection = engine.connection
    with _snapshot(connection) as cache_allowed:
        account = _account(connection, account_id=account_id) if account_id is not None else None
        identity = _identity(connection, candidate_id) if candidate_id is not None else None
        requested_identity = identity
        key = account["website_host"] if account else identity["company_key"]
        row = connection.execute(
            f"SELECT {_columns(mode)} FROM {projection_from(mode)} WHERE r.company_key=?",
            (key,),
        ).fetchone()
        projection = dict(row) if row else None
        current = _current(projection, now, mode)
        if current:
            identity = _identity(connection, projection["candidate_id"])
        elif account is not None:
            # A saved candidate link may now describe a different redirected company.
            member = connection.execute(
                "SELECT m.candidate_id FROM engine_ranking_members m "
                "JOIN snapshots s ON s.id=m.snapshot_id "
                "JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
                "WHERE m.company_key=? ORDER BY ti.live DESC,s.observed_at DESC,"
                "s.id DESC,m.candidate_id LIMIT 1",
                (key,),
            ).fetchone()
            identity = _identity(connection, member[0]) if member else None
        if identity and identity["company_key"] != key:
            identity = (
                requested_identity
                if requested_identity and requested_identity["company_key"] == key
                else None
            )
            current = False
        account = account or _account(connection, company_key=key)
        manual_email = connection.execute(
            "SELECT observed_at FROM company_email_lookups WHERE company_key=? "
            "AND julianday(observed_at)<=julianday(?) "
            "ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1", (key, now.isoformat()),
        ).fetchone()
        email_lookup = {
            "configured": resolve_treg_credentials() is not None,
            "last_ran_at": manual_email[0] if manual_email else None,
        }
        if os.getenv("MICROLINK_API_KEY", "").strip():
            email_lookup["microlink_configured"] = True
        scan_dates = _scan_dates(connection, key, identity, now)
        has_saved_prospects = bool(connection.execute(
            "SELECT EXISTS(SELECT 1 FROM outbound_people WHERE company_key=?)", (key,)
        ).fetchone()[0])
        revisions = [
            tuple(r)
            for r in connection.execute(
                "SELECT m.candidate_id,m.snapshot_id,COALESCE(v.revision,0) "
                "FROM engine_ranking_members m LEFT JOIN engine_ranking_revisions v "
                "ON v.candidate_id=m.candidate_id WHERE m.company_key=? ORDER BY m.candidate_id",
                (key,),
            )
        ]
        # Backfill may not yet have created membership for a newly inspected candidate.
        direct_revisions = []
        for cid in {candidate_id, identity["id"] if identity else None} - {None}:
            row = connection.execute(
                "SELECT revision FROM engine_ranking_revisions WHERE candidate_id=?", (cid,)
            ).fetchone()
            direct_revisions.append((cid, row[0] if row else 0))
        revision = hashlib.sha256(
            repr(
                (
                    policy_version(),
                    mode,
                    identity,
                    projection,
                    current,
                    account,
                    revisions,
                    sorted(direct_revisions),
                    email_lookup,
                    scan_dates,
                    has_saved_prospects,
                )
            ).encode()
        ).hexdigest()
        selection = (
            ("account", account_id) if account_id is not None else ("candidate", candidate_id)
        )
        selection = (*selection, mode)
        cache_key = _cache_key(engine.store, selection) if cache_allowed else None
        cached = _get(cache_key, revision, now)
        if cached is not None:
            return cached

        public, title, role = {}, "", ""
        if identity and identity["snapshot_id"]:
            source = connection.execute(
                "SELECT json_extract(s.observation_json,'$.contacts'),"
                "json_extract(s.observation_json,'$.title'),"
                "json_extract(ti.hypothesis_json,'$.company_role.role') "
                "FROM snapshots s LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id "
                "WHERE s.id=?",
                (identity["snapshot_id"],),
            ).fetchone()
            public, title, role = json.loads(source[0] or "{}"), source[1] or "", source[2] or ""
            if not isinstance(public, dict):
                public = {}
        if current:
            role = (
                connection.execute(
                    "SELECT json_extract(target_json,'$.company_role.role') "
                    "FROM engine_company_ranking WHERE company_key=?",
                    (key,),
                ).fetchone()[0]
                or role
            )
        licensed = (_licensed if _licensed is not None
                    else engine.store.treg_people_intelligence(key, as_of=now))
        from ..microlink_hydration import public_contacts

        public = public_contacts(connection, key, public, now=now)
        observed = identity["observed_at"] if identity else None
        contacts = recommend_contacts(
            {
                "company_key": key,
                "as_of": now.isoformat(),
                "observed_at": observed,
                "role": {"role": role},
                "public_contacts": public,
                "licensed_professional_contacts": licensed,
            },
            limit=COMPANY_CONTACT_DISPLAY_LIMIT,
        )
        expires = _contact_expiry(connection, key, public, licensed, observed, now)
        if current:
            expires = min(expires, _date(projection["valid_until"]))
        elif projection and (future := _date(projection["as_of"])) and future > now:
            expires = min(expires, future)
        payload = {
            "candidate_id": identity["id"] if identity else None,
            "company_key": key,
            "company_name": projection["company_name"]
            if current
            else (
                account["company"]
                if account
                else concise_company_name(
                    label_hint=(identity["label_hint"] or "") if identity else "",
                    title=title,
                    canonical_domain=key,
                    hostname=identity["hostname"] if identity else key,
                )
            ),
            "sales_priority": {
                "score": projection["score"] if current else None,
                "version": score_version(mode, policy_version()),
            },
            "score_breakdown": beings_breakdown(connection, key, mode) if current else None,
            "contact_recommendations": contacts,
            "has_saved_prospects": has_saved_prospects,
            "email_lookup": email_lookup,
            "scan_dates": scan_dates,
            "email_lookup_estimate": email_lookup_estimate(licensed),
            "account": {k: v for k, v in account.items() if k != "website_host"}
            if account
            else None,
            "as_of": projection["as_of"] if current else None,
            "observed_at": observed,
            "evidence_version": projection["evidence_version"] if current else revision,
            "policy_version": policy_version(),
            "ranking_state": "current"
            if current
            else ("pending" if identity and identity["snapshot_id"] else "uninspected"),
            "valid_until": expires.isoformat(),
        }
        return _put(cache_key, revision, now, expires, payload)


def _visible_row(row, now, mode="balanced"):
    row = dict(row)
    current = _current(row, now, mode)
    return {
        "candidate_id": row["candidate_id"],
        "company_key": row["company_key"],
        "company_name": row["company_name"],
        "sales_priority": {"score": row["score"] if current else None,
                           "version": score_version(mode, policy_version())},
        "as_of": row["as_of"] if current else None,
        "evidence_version": row["evidence_version"] if current else None,
        "ranking_state": "current" if current else "pending",
        "valid_until": row["valid_until"] if current else None,
    }


def compact_queue(engine, *, tag="", search="", limit=30, prospects_only=False, reference_time=None,
                  mode="balanced", offset=0, _optimized=True):
    from . import list_cache

    now = _now(reference_time)
    validate_mode(mode)
    kwargs = dict(search=search, tag=tag, limit=limit, prospects_only=prospects_only,
                  reference_time=now, mode=mode, offset=offset, _optimized=_optimized)
    # Neither caller-owned writes nor anonymous profiling connections enter shared caches.
    store = getattr(engine, "store", None)
    if tag or not _optimized or store is None or engine.connection.in_transaction:
        return _uncached_compact_queue(engine, **kwargs)
    key = _cache_key(store, ("queue", search.strip(), max(1, min(limit, 250)),
                             bool(prospects_only), mode, max(0, offset),
                             policy_version(), LENS_VERSION))
    with (list_cache.flight(key, getattr(engine, "_read_cancel", None)),
          _snapshot(engine.connection)):
        revision = list_cache.state(engine.connection)
        if revision is None:
            return _uncached_compact_queue(engine, **kwargs)
        cached = list_cache.get(key, revision, now)
        if cached is not None:
            return cached
        payload = _uncached_compact_queue(engine, **kwargs)
        expires = list_cache.deadline(engine.connection, now, _date(payload["valid_until"]))
        list_cache.put(key, revision, now, expires, payload)
        return payload


def _uncached_compact_queue(engine, *, tag="", search="", limit=30, prospects_only=False,
                            reference_time=None,
                  mode="balanced", offset=0, _optimized=True):
    """Read only visible columns from the same inventory-wide ranking indexes."""
    now = _now(reference_time)
    validate_mode(mode)
    limit, offset = max(1, min(limit, 250)), max(0, offset)
    args = [policy_version(), now.isoformat(), now.isoformat()]
    conditions = ["r.policy_version=?", "r.dirty=0", "r.valid_until>?", "r.as_of<=?"]
    from ..company_tags import normalize_tag, tag_condition

    tag = normalize_tag(tag)
    search = search.strip()
    if search:
        conditions.append(
            "(r.company_key LIKE ? OR r.company_name LIKE ? OR EXISTS("
            "SELECT 1 FROM engine_ranking_members m JOIN candidates c "
            "ON c.id=m.candidate_id WHERE m.company_key=r.company_key "
            "AND c.hostname LIKE ?))"
        )
        args.extend([f"%{search}%"] * 3)
    if tag:
        conditions.append(tag_condition("r.company_key"))
        args.append(tag)
    if prospects_only:
        conditions.append(
            "NOT EXISTS(SELECT 1 FROM sales_accounts a WHERE a.website_host=r.company_key)"
        )
    index = "idx_engine_company_ranking_search" if search else "idx_engine_company_ranking_scores"
    source = f"engine_company_ranking r INDEXED BY {index}"
    count_source = source
    order = "r.score DESC,r.company_key"
    if mode != "balanced":
        from .ranking_modes import match_sql

        source = (f"engine_company_lenses l INDEXED BY idx_engine_lens_{COLUMNS[mode]} "
                  f"JOIN engine_company_ranking r ON {match_sql()}")
        count_source += f" CROSS JOIN engine_company_lenses l ON {match_sql()}"
        if not search:
            # Use the base projection for filtered reads; classification does not
            # control membership in the company ranking.
            source = count_source
        conditions.append("l.version=?")
        args.append(LENS_VERSION)
        order = f"l.{COLUMNS[mode]} DESC,l.company_key"
    from .read_indexes import indexed_search, pending_search_sql, ranking_search_sql, ready

    optimized = _optimized and ready(engine.connection)
    search_index = optimized and bool(search) and indexed_search(engine.connection, search)
    ranked_filter = " AND r.company_key IN (" + ranking_search_sql() + ")" if search_index else ""
    pending_filter = " AND c.id IN (" + pending_search_sql() + ")" if search_index else ""
    filter_args = [f"%{search}%"] * 3 if search_index else []
    if search_index:
        # Drive from matching company keys rather than walking the global lens-score index.
        source = "engine_company_ranking r INDEXED BY sqlite_autoindex_engine_company_ranking_1"
        if mode != "balanced":
            from .ranking_modes import match_sql
            source += " JOIN engine_company_lenses l ON " + match_sql()
        count_source = source
        conditions.append("r.company_key IN (" + ranking_search_sql() + ")")
        args.extend(filter_args)
    elif optimized and not search and not tag:
        column = "score" if mode == "balanced" else COLUMNS[mode]
        listing = f"engine_company_listing q INDEXED BY idx_engine_listing_{column}"
        source = listing + " JOIN engine_company_ranking r ON r.company_key=q.company_key"
        count_source = listing
        if mode != "balanced":
            from .ranking_modes import match_sql
            source += " JOIN engine_company_lenses l ON " + match_sql()
            conditions.pop()  # l.version is represented by the exact revision's lens_ready.
            args.pop()
            conditions.append("q.lens_ready=1")
        conditions = [condition.replace("r.", "q.") for condition in conditions]
        order = f"q.{column} DESC,q.company_key"
    with _snapshot(engine.connection):
        total, items = 0, []

        def append_page(query, parameters, ordering, convert, *, count_query=None):
            # Page across ordered cohorts, including unscored search results, without
            # applying an offset twice or losing records at a cohort boundary.
            nonlocal total
            count = engine.connection.execute(
                count_query or "SELECT COUNT(*) FROM (" + query + ")", parameters
            ).fetchone()[0]
            skip = max(0, offset - total)
            total += count
            if skip < count and len(items) < limit:
                rows = engine.connection.execute(
                    query + f" ORDER BY {ordering} LIMIT ? OFFSET ?",
                    (*parameters, limit-len(items), skip),
                ).fetchall()
                items.extend(convert(row) for row in rows)

        where = " WHERE " + " AND ".join(conditions)
        append_page(
            f"SELECT {_columns(mode)} FROM {source}" + where, args, order,
            lambda row: _visible_row(row, now, mode),
            # Counting starts at filtered base rows rather than scanning every lens score.
            count_query=f"SELECT COUNT(*) FROM {count_source}" + where,
        )
        if mode != "balanced" and (search or tag):
            missing_source = projection_from(mode)
            if search_index:
                missing_source = missing_source.replace(
                    "engine_company_ranking r", "engine_company_ranking r "
                    "INDEXED BY sqlite_autoindex_engine_company_ranking_1", 1)
            # Explicit search must also retain companies whose new lens is still pending.
            append_page(
                f"SELECT {_columns(mode)} FROM {missing_source} WHERE "
                "r.policy_version=? AND r.dirty=0 AND r.valid_until>? AND r.as_of<=? "
                "AND l.company_key IS NULL AND (r.company_key LIKE ? OR r.company_name LIKE ? "
                "OR EXISTS(SELECT 1 FROM engine_ranking_members m JOIN candidates c "
                "ON c.id=m.candidate_id WHERE m.company_key=r.company_key AND c.hostname LIKE ?)) "
                + ranked_filter
                + ((" AND " + tag_condition("r.company_key") + " ") if tag else "")
                + "AND (?=0 OR NOT EXISTS(SELECT 1 FROM sales_accounts a "
                "WHERE a.website_host=r.company_key))",
                (policy_version(), now.isoformat(), now.isoformat(),
                 *([f"%{search}%"] * 3), *filter_args, *([tag] if tag else []),
                 int(prospects_only)), "r.company_key", lambda row: _visible_row(row, now, mode),
            )
        if search or tag:
            append_page(
                "SELECT MIN(c.id) AS candidate_id,"
                "COALESCE(m.company_key,c.hostname) AS company_key,"
                "MAX(m.snapshot_id) AS snapshot_id FROM candidates c "
                "LEFT JOIN engine_ranking_members m ON m.candidate_id=c.id "
                "LEFT JOIN engine_company_ranking r ON r.company_key=m.company_key "
                "WHERE (c.hostname LIKE ? OR m.company_key LIKE ? OR c.label_hint LIKE ?) "
                + pending_filter
                + ((" AND " + tag_condition("COALESCE(m.company_key,c.hostname)") + " ")
                   if tag else "")
                + "AND (r.company_key IS NULL OR r.dirty=1 "
                "OR r.policy_version<>? OR r.valid_until<=? OR r.as_of>?) "
                "AND (?=0 OR NOT EXISTS(SELECT 1 FROM sales_accounts a "
                "WHERE a.website_host=COALESCE(m.company_key,c.hostname))) "
                "GROUP BY COALESCE(m.company_key,c.hostname)",
                (
                    *([f"%{search}%"] * 3),
                    *filter_args,
                    *([tag] if tag else []),
                    policy_version(),
                    now.isoformat(),
                    now.isoformat(),
                    int(prospects_only),
                ),
                "company_key", lambda row: {
                    "candidate_id": row["candidate_id"],
                    "company_key": row["company_key"],
                    "company_name": row["company_key"],
                    "sales_priority": {"score": None},
                    "as_of": None,
                    "evidence_version": None,
                    "valid_until": None,
                    "ranking_state": "pending" if row["snapshot_id"] else "uninspected",
                }
            )
        state = engine.connection.execute(
            "SELECT complete FROM engine_ranking_backfill WHERE policy_version=?",
            (policy_version(),),
        ).fetchone()
        pending = engine.connection.execute(
            "SELECT COUNT(*) FROM engine_ranking_pending"
        ).fetchone()[0]
        if mode != "balanced":
            if _optimized:
                pending += missing_lens_count(
                    engine.connection, base_version=policy_version(), now=now)
            else:
                from .ranking_modes import match_sql
                pending += engine.connection.execute(
                    "SELECT COUNT(*) FROM engine_company_ranking r "
                    "LEFT JOIN engine_company_lenses l ON "
                    + match_sql() + " WHERE l.company_key IS NULL AND r.policy_version=? "
                    "AND r.dirty=0 AND r.valid_until>? AND r.as_of<=?",
                    (policy_version(), now.isoformat(), now.isoformat()),
                ).fetchone()[0]
        expired = engine.connection.execute(
            "SELECT COUNT(*) FROM engine_company_ranking WHERE dirty=0 AND valid_until<=?",
            (now.isoformat(),),
        ).fetchone()[0]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "coverage": {
            "backfill_complete": bool(state and state[0]),
            "pending_companies": pending,
            "expired_companies": expired,
            "more_may_exist": bool(pending or expired or not state or not state[0]),
            "policy_version": policy_version(),
        },
        "valid_until": min(
            [
                _list_deadline(now),
                *[_date(row["valid_until"]) for row in items if row["valid_until"]],
            ]
        ).isoformat(),
    }


def compact_pipeline(engine, *, tag="", search="", stage="", limit=30, offset=0,
                     reference_time=None,
                     mode="balanced"):
    now = _now(reference_time)
    validate_mode(mode)
    limit, offset = max(1, min(limit, 250)), max(0, offset)
    query = """WITH companies AS (
        SELECT id,company,website_host,lifecycle,updated_at,
               ROW_NUMBER() OVER (PARTITION BY website_host ORDER BY id DESC) AS n
        FROM sales_accounts
    ) SELECT * FROM companies WHERE n=1
        AND (?='' OR lower(company) LIKE ? OR lower(website_host) LIKE ?)
        AND (?='' OR lifecycle=?)"""
    args = (search, f"%{search.lower()}%", f"%{search.lower()}%", stage, stage)
    if tag:
        from ..company_tags import normalize_tag, tag_condition

        query += " AND " + tag_condition("website_host")
        args += (normalize_tag(tag),)
    with _snapshot(engine.connection):
        total = engine.connection.execute("SELECT COUNT(*) FROM (" + query + ")", args).fetchone()[
            0
        ]
        if mode == "balanced":
            accounts = engine.connection.execute(
                query + " ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        else:
            from .ranking_modes import match_sql

            accounts = engine.connection.execute(
                "SELECT a.* FROM (" + query + ") a LEFT JOIN engine_company_ranking r "
                "ON r.company_key=a.website_host LEFT JOIN engine_company_lenses l ON "
                + match_sql() + " ORDER BY CASE WHEN r.policy_version=? AND r.dirty=0 "
                "AND r.valid_until>? AND r.as_of<=? THEN l." + COLUMNS[mode]
                + " END DESC,a.updated_at DESC,a.id DESC LIMIT ? OFFSET ?",
                (*args, policy_version(), now.isoformat(), now.isoformat(), limit, offset),
            ).fetchall()
        items = []
        for account in accounts:
            row = engine.connection.execute(
                f"SELECT {_columns(mode)} FROM {projection_from(mode)} WHERE r.company_key=?",
                (account["website_host"],),
            ).fetchone()
            item = (
                _visible_row(row, now, mode)
                if row
                else {
                    "candidate_id": None,
                    "company_key": account["website_host"],
                    "sales_priority": {"score": None},
                    "ranking_state": "uninspected",
                    "as_of": None,
                    "evidence_version": None,
                    "valid_until": None,
                }
            )
            items.append(
                {
                    **item,
                    "account_id": account["id"],
                    "company_name": account["company"],
                    "stage": account["lifecycle"],
                    "updated_at": account["updated_at"],
                }
            )
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "valid_until": min(
            [
                _list_deadline(now),
                *[_date(row["valid_until"]) for row in items if row["valid_until"]],
            ]
        ).isoformat(),
    }
