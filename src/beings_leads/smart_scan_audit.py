"""Durable Smart Scan timings and a read-only, aggregate run review."""

import argparse
import asyncio
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing, nullcontext
from datetime import datetime
from functools import wraps
from pathlib import Path

from .scan_diagnostics import MARKER, SCAN_ID, source_trace

RESULT_COUNTS = (
    "processed", "discovered", "direct_candidates", "sources_total", "sources_completed",
    "success_count", "failure_count", "completed", "failed", "requests",
    "pages_fetched", "candidates_persisted", "candidates_hydrated",
)
SUCCESS = {"success", "success_empty", "complete"}
TERMINAL = SUCCESS | {
    "not_applicable", "partial", "transient_error", "rate_limited", "robots_blocked",
    "identity_ambiguous", "failed",
}


def _success_rate(outcomes, terminal, success):
    count = sum(outcomes.get(status, 0) for status in terminal)
    return sum(outcomes.get(status, 0) for status in success) / count if count else None


def measure_operation(name, *, child=None, trace_source=False):
    """Measure elapsed time at operation boundaries, including interrupted attempts."""
    def decorate(method):
        @wraps(method)
        async def measured(manager, *args, **kwargs):
            operation = name(*args, **kwargs) if callable(name) else name
            started = time.monotonic()
            state = manager.state
            status = "returned"
            result = None
            token = SCAN_ID.set(state.get("id"))
            try:
                trace = nullcontext()
                if trace_source:
                    request = args[0] if args else kwargs["request"]
                    trace = source_trace(operation, suggestion_ids=request.suggestion_ids)
                with trace as metrics:
                    result = await method(manager, *args, **kwargs)
                    if metrics is not None and isinstance(result, dict):
                        metrics.result.update({
                            key: result[key] for key in (
                                "status", "run_id", "pages_fetched", "documents_fetched",
                                "candidates_persisted", "candidates_hydrated",
                            ) if key in result
                        })
                if isinstance(result, dict):
                    status = str(result.get("status") or status)
                elif isinstance(result, str):
                    status = result
                return result
            except asyncio.CancelledError:
                status = "interrupted"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                SCAN_ID.reset(token)
                if state.get("id") and manager.state.get("id") == state["id"]:
                    audit = manager.state.setdefault("scan_audit", {})
                    entry = audit.setdefault("operations", {}).setdefault(
                        operation, {"elapsed_seconds": 0.0, "outcomes": {}, "results": {}},
                    )
                    seconds = time.monotonic() - started
                    entry["elapsed_seconds"] += seconds
                    entry["longest_attempt_seconds"] = max(
                        seconds, entry.get("longest_attempt_seconds", 0.0),
                    )
                    outcomes = entry["outcomes"]
                    outcomes[status] = outcomes.get(status, 0) + 1
                    if isinstance(result, dict):
                        summary = result.get("summary") or {}
                        for field in RESULT_COUNTS:
                            value = result.get(field, summary.get(field))
                            if type(value) is int:
                                entry["results"][field] = entry["results"].get(field, 0) + value
                        for source in summary.get("source_results", ()):
                            source_status = source.get("status")
                            if source_status:
                                counts = entry.setdefault("source_outcomes", {})
                                counts[source_status] = counts.get(source_status, 0) + 1
                            if type(seconds := source.get("enumeration_seconds")) in {float, int}:
                                entry["source_enumeration_seconds"] = (
                                    entry.get("source_enumeration_seconds", 0.0) + seconds
                                )
                        if child and (run_id := result.get(child) or summary.get(child)):
                            audit.setdefault("child_runs", {})[str(run_id)] = operation
                    manager._persist_state()

        return measured

    return decorate


def _scanner_evidence(connection, executions, scanners):
    from .sales_engine.evidence_accounting import decode_record

    for company, execution_types in executions.items():
        ranking = connection.execute(
            "SELECT as_of,policy_version,evidence_version,target_json "
            "FROM engine_company_ranking WHERE company_key=?", (company,),
        ).fetchone()
        if ranking is None:
            continue
        target = json.loads(ranking["target_json"])
        vector = target.get("opportunity", {})
        dimensions = {key for keys in vector.get("dimension_event_keys", {}).values()
                      for key in keys}
        effects = vector.get("evidence_group_effects", {})
        roots = defaultdict(set)
        dimension_linked = set()
        for row in connection.execute(
            "SELECT record_json FROM engine_evidence_accounting WHERE company_key=? "
            "AND as_of=? AND policy_version=? AND evidence_version=? AND source_kind='frontier'",
            (company, ranking["as_of"], ranking["policy_version"], ranking["evidence_version"]),
        ):
            record = json.loads(decode_record(row[0]))
            reference = record.get("source_reference")
            if not isinstance(reference, dict):
                continue
            scanner = execution_types.get(reference.get("execution_id"))
            if scanner is None or reference.get("table") != "atomic_observations":
                continue
            roots[scanner].update(record.get("causal_roots", ()))
            if dimensions.intersection(record.get("event_keys", ())):
                dimension_linked.add(scanner)
        for scanner, linked in roots.items():
            counts = scanners[scanner]["score_evidence"]
            counts["companies_with_published_receipts"] += 1
            counts["companies_with_dimension_links"] += scanner in dimension_linked
            for root in linked:
                delta = effects.get(root, {}).get("founder_priority")
                direction = ("unmeasured" if delta is None else "positive" if delta > 0
                             else "negative" if delta < 0 else "zero")
                counts[f"{direction}_linked_root_effects"] += 1
                if delta is not None:
                    counts["max_absolute_linked_root_points"] = max(
                        counts["max_absolute_linked_root_points"], abs(delta),
                    )


def _runtime_records(directory, state):
    started = (state.get("started_at") or "")[:10]
    finished = (state.get("finished_at") or "")[:10]
    records = []
    for path in sorted(directory.glob("beings-growth-runtime.log*")):
        suffix = path.name.removeprefix("beings-growth-runtime.log")
        if suffix:
            try:
                day = datetime.strptime(suffix, ".%Y-%m-%d").date().isoformat()
            except ValueError:
                continue
            if (started and day < started) or (finished and day > finished):
                continue
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    _, marker, payload = line.partition(MARKER)
                    if not marker:
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("scan_id") == state["id"]:
                        records.append(event)
        except FileNotFoundError:
            continue  # The daily rotation may have renamed or expired this file.
    return sorted(records, key=lambda event: event.get("at", ""))


def _company_attempts(connection, records):
    registered = {record["run_id"]: record for record in records
                  if record.get("event") == "company_registered"
                  and type(record.get("run_id")) is int}
    return [{**dict(row), **{key: registered[row["run_id"]].get(key)
                            for key in ("job_id", "attempt", "offered_hosts")}}
            for row in connection.execute(
        "SELECT r.id AS run_id,r.status,r.started_at,r.finished_at,"
        "(SELECT count(*) FROM snapshots WHERE run_id=r.id) AS committed_snapshots "
        "FROM runs r WHERE r.id IN (SELECT value FROM json_each(?)) ORDER BY r.id",
        (json.dumps(sorted(registered)),),
    )]


def review(database, scan_id=None, *, runtime_diagnostics=False):
    """Read one saved run and its explicitly linked executions from one database snapshot."""
    from .storage_json import configure

    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True,
                                timeout=5)) as connection:
        configure(connection)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if scan_id:
            row = connection.execute(
                "SELECT state_json FROM smart_scan_runs WHERE id=?", (scan_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT state_json FROM smart_scan_runs WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1",
            ).fetchone()
        if row is None:
            raise ValueError("No matching Smart Scan run")
        state = json.loads(row[0])
        records = (_runtime_records(Path(database).resolve().parent, state)
                   if runtime_diagnostics else [])
        audit = state.get("scan_audit", {})
        children = dict(audit.get("child_runs", {}))
        if run_id := state.get("microlink_hydration_run_id"):
            children[str(run_id)] = "microlink_hydration"
        scanners = defaultdict(lambda: {
            "outcomes": Counter(), "elapsed_seconds": 0.0, "requests": 0,
            "cost_microusd": 0, "observations": 0, "score_evidence": Counter(),
        })
        executions = defaultdict(dict)
        for row in connection.execute(
            "SELECT id,company_key,scanner_id,status,started_at,finished_at,request_count,"
            "cost_microusd,observation_count FROM scanner_executions "
            "WHERE run_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(map(int, children))),),
        ):

            scanner = scanners[row["scanner_id"]]
            scanner["outcomes"][row["status"]] += 1
            if row["finished_at"]:
                scanner["elapsed_seconds"] += max(0.0, (
                    datetime.fromisoformat(row["finished_at"])
                    - datetime.fromisoformat(row["started_at"])
                ).total_seconds())
            scanner["requests"] += row["request_count"]
            scanner["cost_microusd"] += row["cost_microusd"]
            scanner["observations"] += row["observation_count"]
            executions[row["company_key"]][row["id"]] = row["scanner_id"]
        _scanner_evidence(connection, executions, scanners)
        for scanner in scanners.values():
            outcomes = scanner["outcomes"]
            scanner["success_rate"] = _success_rate(
                outcomes, TERMINAL - {"not_applicable"}, SUCCESS,
            )
        website_effects = defaultdict(Counter)
        for row in connection.execute(
            "SELECT s.run_id,r.target_json FROM engine_company_ranking r "
            "JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.run_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(map(int, children))),),
        ):
            opportunity = json.loads(row["target_json"]).get("opportunity", {})
            delta = opportunity.get("source_effects", {}).get("website", {}).get("founder_priority")
            counts = website_effects[children[str(row["run_id"])]]
            counts["companies_with_matching_snapshot"] += 1
            if delta is None:
                counts["unmeasured"] += 1
            else:
                counts["positive" if delta > 0 else "negative" if delta < 0 else "zero"] += 1
                counts["max_absolute_points"] = max(counts["max_absolute_points"], abs(delta))
        operations = audit.get("operations", {})
        child_phases = defaultdict(Counter)
        for child in connection.execute(
            "SELECT id,summary_json FROM runs WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(map(int, children))),),
        ):
            summary = json.loads(child["summary_json"] or "{}")
            child_phases[children[str(child["id"])]].update(summary.get("phase_wall_seconds", {}))
        report = {
            "scan_id": state["id"], "status": state["status"],
            "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
            "operations": [{
                "operation": name, **value,
                "success_rate": _success_rate(
                    value["outcomes"], {"complete", "partial", "failed", "error"}, {"complete"},
                ),
                "child_phase_seconds": child_phases.get(name, {}),
                "website_removal_effects": website_effects.get(name, {}),
            } for name, value in sorted(
                operations.items(), key=lambda pair: -pair[1]["elapsed_seconds"],
            )],
            "child_runs": children,
            "scanners": [{"scanner": name, **value} for name, value in sorted(
                scanners.items(), key=lambda pair: -pair[1]["elapsed_seconds"],
            )],
            "results": {
                "new_companies": state.get("new_companies_added"),
                "errors": state.get("error_total"),
                "counters": {key: value for key, value in state.get("counters", {}).items()
                             if type(value) in {int, float}},
                "provider_spend_usd": {key: value for key, value in state.items()
                                       if key.startswith("actual_") and key.endswith("_usd")
                                       and type(value) in {int, float}},
                "microlink": {key: value for key, value in
                              (state.get("microlink_hydration_result") or {}).items()
                              if type(value) in {int, float}},
            },
            "interpretation": [
                "Operation times include waits and overlap. Do not sum them as total run time.",
                "Returned operations may contain item failures. Consult the result counters.",
                "Score effects are saved founder-priority changes when removing linked evidence "
                "roots. Roots can be shared by scanners. Effects are conditional and non-additive.",
                "Website effects remove the full website source for a matching ranked snapshot. "
                "Score linkage covers matching receipts in the latest saved shared ranking. "
                "Missing links leave influence unmeasured, including older scans.",
                "Scanner success rate excludes running and not-applicable executions "
                "and includes empty successes.",
            ],
        }
        if runtime_diagnostics:
            report["runtime_diagnostics"] = {
                "records": records, "company_attempts": _company_attempts(connection, records),
                "scope": "retained runtime logs; component times can overlap",
            }
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--scan-id")
    parser.add_argument("--runtime-diagnostics", action="store_true")
    args = parser.parse_args()
    try:
        result = review(args.database, args.scan_id, runtime_diagnostics=args.runtime_diagnostics)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
