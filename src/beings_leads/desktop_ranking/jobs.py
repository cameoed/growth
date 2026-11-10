"""Select and conditionally publish desktop work using the existing SQLite queues."""

import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import zip_longest

from ..domain_policy import disallowed_domains
from ..missions import scoring as missions
from ..missions.store import get_mission
from ..sales_engine import ranking, ranking_modes, read_indexes
from ..sales_engine.persistence import SalesEngineRepository
from ..store import (
    DELETE_PROJECTED_EVENTS_SQL,
    PROJECTED_EVENTS_SQL,
    PROJECTED_RELATIONSHIPS_SQL,
    TARGET_PROJECTION_SQL,
)
from .control import MAX_COMPANIES
from .wire import ProtocolError

PROJECTION_SQL = (
    TARGET_PROJECTION_SQL,
    DELETE_PROJECTED_EVENTS_SQL,
    PROJECTED_EVENTS_SQL,
    PROJECTED_RELATIONSHIPS_SQL,
)
SHARED_PER_TURN = 10
MISSIONS_PER_TURN = 4
LENSES_PER_TURN = 16
MISSION_WINDOW = 1536


def _first(connection, sql, parameters):
    row = connection.execute(sql, parameters).fetchone()
    return tuple(row) if row is not None else None


def _revision(connection, cid):
    return (
        _first(
            connection, "SELECT revision FROM engine_ranking_revisions WHERE candidate_id=?", (cid,)
        )
        or (0,)
    )[0]


def _pending(connection, cid):
    row = connection.execute(
        "SELECT enqueued_at,due_at,failures FROM engine_ranking_pending WHERE candidate_id=?",
        (cid,),
    ).fetchone()
    return dict(row) if row is not None else None


def _shared_pin(connection, key):
    return (
        ranking._evidence_version(connection, key),
        _first(
            connection,
            "SELECT candidate_id,snapshot_id,policy_version,evidence_version,"
            "as_of,valid_until,dirty FROM engine_company_ranking WHERE company_key=?",
            (key,),
        ),
    )


def _lens_pin(connection, key):
    return _first(
        connection,
        "SELECT policy_version,evidence_version,as_of,valid_until,dirty "
        "FROM engine_company_ranking WHERE company_key=?",
        (key,),
    )


def _candidate_pin(connection, cid):
    return (
        _revision(connection, cid),
        _first(connection, "SELECT MAX(id) FROM snapshots WHERE candidate_id=?", (cid,))[0],
    )


class ProjectionWrites:
    """Capture only the four existing derived snapshot writes for Oracle to apply."""

    def __init__(self):
        self.commands = []

    def execute(self, sql, values):
        self.executemany(sql, [values])

    def executemany(self, sql, rows):
        self.commands.append((PROJECTION_SQL.index(sql), list(rows)))


def _write_projection(connection, cid, plan_now, commands):
    ids = [command[0] for command in commands]
    if (
        not ids
        or ids[:2] != [0, 1]
        or ids != sorted(set(ids))
        or any(i not in (0, 1, 2, 3) for i in ids)
    ):
        raise ProtocolError("Invalid snapshot projection")
    [[target], [[snapshot]]] = [command[1] for command in commands[:2]]
    if (
        len(target) != 21
        or target[:2] != [snapshot, cid]
        or target[-1] != plan_now.isoformat()
        or not connection.execute(
            "SELECT 1 FROM snapshots WHERE id=? AND candidate_id=?", (snapshot, cid)
        ).fetchone()
    ):
        raise ProtocolError("Snapshot projection has the wrong identity")
    for index, rows in commands:
        if index == 2 and any(row[:3] != [snapshot, cid, target[3]] for row in rows):
            raise ProtocolError("Signal projection has the wrong identity")
        if index == 3 and any(row[0] != target[3] for row in rows):
            raise ProtocolError("Relationship projection has the wrong identity")
        connection.executemany(PROJECTION_SQL[index], rows)


class MissionWindows:
    """Traverse exact company-key ranges without sorting the entire inventory per batch."""

    def __init__(self):
        self.rotation = 0
        self.cursor = {}
        self.retry_after = {}

    def next(self, connection, mission_id, now):
        if self.retry_after.get(mission_id, "") > now.isoformat():
            return None
        after = self.cursor.get(mission_id, "")
        end = connection.execute(
            "SELECT company_key FROM engine_mission_inventory WHERE company_key>? "
            "ORDER BY company_key LIMIT 1 OFFSET ?",
            (after, MISSION_WINDOW - 1),
        ).fetchone()
        if end is None:
            end = connection.execute(
                "SELECT company_key FROM engine_mission_inventory WHERE company_key>? "
                "ORDER BY company_key DESC LIMIT 1",
                (after,),
            ).fetchone()
        if end is None:
            self.cursor[mission_id] = ""
            self.retry_after[mission_id] = (now + timedelta(seconds=10)).isoformat()
            return None
        return after, end[0]


class Plan:
    def __init__(self, store, request, windows, *, now=None, limit=MAX_COMPANIES):
        self.store = store
        self.connection = store.connection
        self.repo = SalesEngineRepository(self.connection)
        self.request = request
        self.kind = request.kind if request else "background"
        self.options = request.parameters if request else {}
        stamp = self.options.get("now")
        self.now = ranking._time(datetime.fromisoformat(stamp) if stamp else now)
        self.identity = store._database_identity()
        self.domains = disallowed_domains()
        self.tasks = {}
        self.windows = windows
        self.scanning_missions = False
        self.window_starts = {}
        self.shared = {}
        self.lenses = {}
        self.projections = {}
        self.projection_pending = {}
        self.pending = {}
        self.finished = {}
        self.shared_outcomes = {}
        self.total_shared = 0
        self.checked_shared = 0
        self.stats = {
            "selected": 0,
            "refreshed": 0,
            "reused": 0,
            "discarded": 0,
            "errors": [],
            "as_of": self.now.isoformat(),
            "version": ranking.policy_version(),
            "provider_requests": 0,
            "mission_refreshed": 0,
            "lens_refreshed": 0,
            "shared_companies_refreshed": 0,
            "reprojected": [],
        }
        self.busy = False
        if self.kind == "background":
            pending = self.connection.execute(
                "SELECT candidate_id FROM engine_desktop_projection_pending "
                "WHERE due_at<=? ORDER BY due_at,candidate_id LIMIT ?",
                (self.now.isoformat(), limit),
            )
            ids = [row[0] for row in pending]
            if ids:
                self._select_projections(ids)
                self.busy = True
                return
        if self.kind in {"background", "refresh", "candidates"}:
            self._select_shared(min(limit, SHARED_PER_TURN) if not request else limit)
        if self.kind in {"background", "mission"}:
            self._select_missions(limit)
        if self.kind == "background" and (room := limit - len(self.tasks)) > 0:
            self._select_lenses(min(room, LENSES_PER_TURN))
        if self.kind == "reproject":
            self._select_projections(self.options["candidate_ids"])
        if self.kind == "target":
            task = self._task("target")
            task["target"] = self.options
            self.projections["target"] = _candidate_pin(
                self.connection, self.options["candidate_id"]
            )
            task["candidate_pin"] = self.projections["target"]
        self.busy = bool(
            self.scanning_missions
            or self.tasks
            or self.stats["selected"]
            or self.stats.get("seeded")
            or self.stats.get("backfill_complete") is False
        )

    def work(self):
        # Shared jobs still get a turn, while cheap mission work can commit immediately.
        if self.request is not None:
            return list(self.tasks.values())
        mission = [task for task in self.tasks.values() if "missions" in task]
        other = [task for task in self.tasks.values() if "missions" not in task]
        return [task for pair in zip_longest(mission, other) for task in pair if task is not None]

    def _task(self, key):
        return self.tasks.setdefault(key, {"key": key})

    def _select_projections(self, ids):
        for cid in ids:
            key = f"candidate:{cid}"
            self.projections[key] = _candidate_pin(self.connection, cid)
            self.projection_pending[key] = self._projection_pending(cid)
            self._task(key).update(candidate=cid, candidate_pin=self.projections[key])

    def _projection_pending(self, cid):
        return _first(
            self.connection,
            "SELECT enqueued_at,failures FROM engine_desktop_projection_pending "
            "WHERE candidate_id=?",
            (cid,),
        )

    def _select_shared(self, limit):
        c = self.connection
        targeted = self.kind == "candidates"
        reuse_current = not targeted or self.options.get("reuse_current", False)
        with self.repo._transaction():
            if targeted:
                ids = self.options["candidate_ids"]
            else:
                rows, backfill = ranking.select_pending(
                    self.store,
                    min(limit, self.options.get("limit", limit)),
                    self.now,
                    prioritize_expired_scores=self.options.get("prioritize", not self.request),
                )
                self.stats.update(backfill)
                ids = [row["candidate_id"] for row in rows]
            affected = {}
            for cid in ids:
                try:
                    with self.repo._transaction():
                        affected[cid] = ranking.prepare_identity(self.store, cid)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    pending = _pending(c, cid)
                    if pending is not None:
                        ranking.fail_pending(
                            c, cid, pending, _revision(c, cid), self.now, type(exc).__name__
                        )
                    self.stats["errors"].append({"candidate_id": cid, "error": type(exc).__name__})
            self.total_shared = len(ids)
            self.checked_shared = len(ids) - len(affected)
            self.stats["selected"] = len(ids)
            pins = {}
            for cid, keys in affected.items():
                self.pending[cid] = (_pending(c, cid), _revision(c, cid), keys)
                for key in sorted(keys):
                    if key in pins:
                        continue
                    pins[key] = _shared_pin(c, key)
                    if reuse_current and ranking._publication_is_current(
                        c,
                        key,
                        self.now,
                        self.stats["version"],
                    ):
                        self.shared_outcomes[key] = "reused"
                    else:
                        self.shared[key] = pins[key]
                        self._task(key)["shared"] = pins[key][0]
            self._finish_pending()

    def _select_missions(self, limit):
        if self.request:
            ids = [self.options["mission_id"]]
            per_mission = min(limit, self.options.get("limit", limit))
        else:
            ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT id FROM engine_missions WHERE archived=0 AND id<>'beings' "
                    "ORDER BY created_at,id"
                )
            ]
            if not ids:
                return
            mission_count = len(ids)
            start = self.windows.rotation % mission_count
            ids = (ids[start:] + ids[:start])[:MISSIONS_PER_TURN]
            self.windows.rotation = (start + len(ids)) % mission_count
            per_mission = limit
        rows_by_mission = []
        pins = []
        for mission_id in ids:
            try:
                mission = get_mission(self.connection, mission_id)
            except LookupError:
                continue
            if mission["spec"].get("compiler_pending") or mission["spec"].get("compiler_error"):
                continue
            # Send the pinned rubric, not redundant source prompt or compiler receipts.
            pin = {field: mission[field] for field in ("id", "revision", "spec")}
            pins.append(pin)
            window = None
            if self.request is None:
                window = self.windows.next(self.connection, mission_id, self.now)
                if window is None:
                    continue
                self.scanning_missions = True
                self.window_starts[mission_id] = window[0]
            rows = missions._select_rows(
                self.store,
                mission,
                per_mission,
                self.now,
                self.options.get("company_key"),
                company_keys=self.options.get("company_keys"),
                force=self.options.get("force", False),
                window=window,
            )
            rows_by_mission.append((pin, rows, window))

        # A company uses one worker slot even if several missions need it.
        admitted = [0] * len(rows_by_mission)
        for offset, turn in enumerate(zip_longest(*(rows for _, rows, _ in rows_by_mission))):
            for index, row in enumerate(turn):
                if row is None or admitted[index] != offset:
                    continue
                key = row["company_key"]
                if key not in self.tasks and len(self.tasks) >= limit:
                    continue
                pin, _, _ = rows_by_mission[index]
                self._task(key).setdefault("missions", []).append((pin, row))
                admitted[index] += 1
                if self.request:
                    self.stats["selected"] += 1

        for (pin, rows, window), count in zip(rows_by_mission, admitted, strict=True):
            if window is None:
                continue
            if count == len(rows) and count < per_mission:
                self.windows.cursor[pin["id"]] = window[1]
            elif count:
                self.windows.cursor[pin["id"]] = rows[count - 1]["company_key"]

        if self.request is None and self.tasks:
            # Mission cursors can diverge. Reuse these companies for other due
            # missions without advancing their cursors past unselected work.
            for pin in pins:
                keys = [
                    key for key, task in self.tasks.items()
                    if all(job_pin["id"] != pin["id"] for job_pin, _ in task.get("missions", ()))
                ]
                if not keys:
                    continue
                for row in missions._select_rows(
                    self.store, pin, len(keys), self.now, None, company_keys=keys,
                ):
                    self._task(row["company_key"]).setdefault("missions", []).append((pin, row))

    def _select_lenses(self, limit):
        if read_indexes.ready(self.connection):
            # Stale base rows are re-ranked with their lenses, so only current gaps need repair.
            rows = self.connection.execute(
                "SELECT company_key FROM engine_company_listing INDEXED BY idx_engine_listing_gaps "
                "WHERE policy_version=? AND dirty=0 AND lens_ready=0 AND valid_until>? "
                "AND as_of<=? LIMIT ?",
                (self.stats["version"], self.now.isoformat(), self.now.isoformat(), limit),
            )
        else:
            rows = self.connection.execute(
                "SELECT r.company_key FROM engine_company_ranking r "
                "LEFT JOIN engine_company_lenses l ON "
                + ranking_modes.match_sql()
                + " WHERE l.company_key IS NULL ORDER BY r.company_key LIMIT ?",
                (limit,),
            )
        for row in rows:
            key = row[0]
            if key in self.shared:
                continue
            self.lenses[key] = _lens_pin(self.connection, key)
            self._task(key)["lens"] = True

    def _finish_pending(self):
        for cid, (pending, revision, keys) in list(self.pending.items()):
            if not keys <= self.shared_outcomes.keys():
                continue
            del self.pending[cid]
            self.checked_shared += 1
            if (
                _pending(self.connection, cid) != pending
                or _revision(self.connection, cid) != revision
            ):
                self.stats["discarded"] += 1
                continue
            outcomes = {self.shared_outcomes[key] for key in keys}
            if "discarded" in outcomes:
                self.stats["discarded"] += 1
                continue
            errors = outcomes - {"refreshed", "reused"}
            if errors:
                error = min(errors)
                if pending is not None:
                    ranking.fail_pending(self.connection, cid, pending, revision, self.now, error)
                self.stats["errors"].append({"candidate_id": cid, "error": error})
                continue
            if pending is not None:
                self.connection.execute(
                    "DELETE FROM engine_ranking_pending WHERE candidate_id=?",
                    (cid,),
                )
            self.stats["refreshed" if "refreshed" in outcomes else "reused"] += 1

    def _publish_shared(self, key, outcome, publication_time):
        if key not in self.shared:
            raise ProtocolError("Unknown shared ranking")
        expected = self.shared[key]
        if _shared_pin(self.connection, key) != expected:
            return "discarded"
        if "error" in outcome:
            return outcome["error"]
        result = outcome["result"]
        if result is None:
            # Empty identities must remain empty, including in the scoring snapshot.
            if self.connection.execute(
                "SELECT 1 FROM engine_ranking_members WHERE company_key=?",
                (key,),
            ).fetchone():
                return "discarded"
        else:
            values, lenses, packets = result
            if (
                values[0] != key
                or values[7:10]
                != [
                    self.stats["version"],
                    expected[0],
                    self.now.isoformat(),
                ]
                or datetime.fromisoformat(values[10]) <= publication_time
                or lenses[:5] != [key, ranking_modes.VERSION, *values[7:10]]
                or any(packet[:2] != [key, index] for index, packet in enumerate(packets))
            ):
                return "discarded"
            result = (
                values,
                lenses,
                [(*row[:3], base64.b64decode(row[3], validate=True)) for row in packets],
            )
        ranking._write_company(self.connection, key, result, self.now, prepared_packets=True)
        self.stats["shared_companies_refreshed"] += 1
        return "refreshed"

    def _publish_one(self, key, outcome, publication_time):
        task = self.tasks[key]
        if "shared" in task:
            try:
                with self.repo._transaction():
                    self.shared_outcomes[key] = self._publish_shared(
                        key,
                        outcome["shared"],
                        publication_time,
                    )
            except sqlite3.IntegrityError as exc:
                self.shared_outcomes[key] = type(exc).__name__
        if "missions" in task:
            results = outcome["missions"]
            for (mission, row), result in zip(task["missions"], results, strict=True):
                if result.get("review_pin") is not None:
                    result["review_pin"] = tuple(result["review_pin"])
                if not missions._publish(
                    self.store,
                    row,
                    mission,
                    result,
                    self.now,
                    publication_time,
                    prepared_audit=True,
                ):
                    self.stats["discarded"] += 1
                    if mission["id"] in self.window_starts:
                        self.windows.cursor[mission["id"]] = self.window_starts[mission["id"]]
                elif "error" in result:
                    self.stats["errors"].append({"company_key": key, "error": result["error"]})
                else:
                    self.stats["mission_refreshed"] += 1
        if "lens" in task:
            values = outcome["lens"]
            pin = self.lenses[key]
            if (
                values is not None
                and values[:5] == [key, ranking_modes.VERSION, *pin[:3]]
                and _lens_pin(self.connection, key) == pin
            ):
                ranking_modes.write_lenses(self.connection, values)
                self.stats["lens_refreshed"] += 1
        if "candidate" in task:
            cid = task["candidate"]
            if (
                _candidate_pin(self.connection, cid) != self.projections[key]
                or self._projection_pending(cid) != self.projection_pending[key]
                or outcome.get("stale")
            ):
                self.stats["discarded"] += 1
                return
            if "error" in outcome:
                if (pending := self.projection_pending[key]) is not None:
                    failures = pending[1] + 1
                    retry = publication_time + timedelta(minutes=min(1440, 2 ** min(failures, 11)))
                    self.connection.execute(
                        "UPDATE engine_desktop_projection_pending SET failures=?,due_at=? "
                        "WHERE candidate_id=?",
                        (failures, retry.isoformat(), cid),
                    )
                self.stats["errors"].append({"candidate_id": cid, "error": outcome["error"]})
                return
            if commands := outcome["projection"]:
                _write_projection(self.connection, cid, self.now, commands)
                self.stats["reprojected"].append(cid)
            self.connection.execute(
                "DELETE FROM engine_desktop_projection_pending WHERE candidate_id=?",
                (cid,),
            )
        if "target" in task:
            cid = task["target"]["candidate_id"]
            if (
                _candidate_pin(self.connection, cid) == self.projections[key]
                and "target" in outcome
            ):
                self.stats["target"] = outcome["target"]

    def publish(self, packets, *, publication_time=None):
        publication_time = publication_time or datetime.now(UTC)
        with self.repo._transaction():
            current = self.store._database_identity() == self.identity
            current = current and self.domains == disallowed_domains()
            current = current and (self.request is None or not self.request.future.cancelled())
            for key, outcome in packets:
                if key not in self.tasks:
                    raise ProtocolError("Unknown ranking work item")
                if key in self.finished:
                    continue
                if current:
                    self._publish_one(key, outcome, publication_time)
                else:
                    self.stats["discarded"] += 1
                    if key in self.shared:
                        self.shared_outcomes[key] = "discarded"
                self.finished[key] = True
            self._finish_pending()

    def result(self):
        if self.kind == "mission":
            self.stats["refreshed"] = self.stats["mission_refreshed"]
        return {**self.stats, "busy": self.busy}
