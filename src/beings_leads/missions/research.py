"""Select due website work for a custom mission without a provider or score write."""

from datetime import timedelta

from ..domain_admission import is_disallowed
from ..store import COMPANY_SCAN_EFFORT, COMPANY_SCAN_FRESH_HOURS
from .reads import _score_available, _selection
from .store import get_mission


def website_cohort(connection, mission_id, now, limit):
    if mission_id in {"beings", "all"}:
        raise ValueError("Select a custom mission")
    mission = get_mission(connection, mission_id)
    if not _score_available(mission):
        raise ValueError("Complete mission setup first")
    _, current, args = _selection(connection, mission_id, now)
    has_discovery = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='microlink_discovery_scan_plans'"
    ).fetchone()
    discovery = """SELECT DISTINCT l.candidate_id FROM microlink_discovery_scan_plans p
        JOIN microlink_discovery_requests q ON q.budget_id=p.budget_id
        JOIN microlink_discovery_leads l ON l.query_key=q.query_key
        WHERE json_extract(p.plan_json,'$.mission_id')=?
        AND json_extract(p.plan_json,'$.lane')='mission'""" if has_discovery else (
        "SELECT NULL AS candidate_id WHERE ? IS NULL"
    )
    rows = connection.execute(
        f"""WITH discovered AS MATERIALIZED ({discovery}),
        candidates_due AS (
            SELECT i.company_key,c.hostname,
                   CASE WHEN {current} THEN s.score END AS current_score,
                   s.score AS last_score,i.snapshot_id,
                   EXISTS (SELECT 1 FROM engine_mission_members m
                           JOIN discovered d ON d.candidate_id=m.candidate_id
                           WHERE m.company_key=i.company_key) AS discovered
            FROM engine_mission_inventory i JOIN candidates c ON c.id=i.candidate_id
            LEFT JOIN engine_mission_scores s ON s.company_key=i.company_key AND s.mission_id=?
            LEFT JOIN company_scan_work w ON w.company_key=i.company_key AND w.effort_key=?
            WHERE (w.lease_expires_at IS NULL OR w.lease_expires_at<=? OR w.status<>'running')
              AND (w.next_retry_at IS NULL OR w.next_retry_at<=? OR w.status<>'failed')
              AND (w.completed_at IS NULL OR w.completed_at<=? OR w.status<>'complete'
                   OR w.completed_snapshot_id IS NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM engine_mission_members m
                  JOIN prospect_workflow p ON p.candidate_id=m.candidate_id
                  WHERE m.company_key=i.company_key AND (p.excluded=1 OR p.scan_enabled=0)
              )
        )
        SELECT company_key,hostname FROM candidates_due
        WHERE current_score>0 OR (
            current_score IS NULL AND (last_score>0 OR discovered)
        )
        ORDER BY CASE WHEN discovered AND snapshot_id IS NULL THEN 0
                      WHEN current_score>0 THEN 1 ELSE 2 END,
                 COALESCE(current_score,last_score) DESC,company_key""",
        [
            mission_id, *args, mission_id, COMPANY_SCAN_EFFORT, now.isoformat(), now.isoformat(),
            (now - timedelta(hours=COMPANY_SCAN_FRESH_HOURS)).isoformat(),
        ],
    )
    hosts = []
    for key, host in rows:
        if is_disallowed(key) or is_disallowed(host):
            continue
        hosts.append(host)
        if len(hosts) == limit:
            break
    return hosts
