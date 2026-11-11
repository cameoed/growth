# Mission-aware scan execution

## Purpose
All scan-menu requests carry the selected mission. Use its saved compiled keywords,
criteria and metrics as research preferences and its existing ranking for applicable
company cohorts. Never interpret Beings-specific buyer rules as custom-mission truth.

## Implementation
Add a provider-free mission scan context helper under missions/ using get_mission,
existing mission_terms and existing mission-ranked reads. Thread mission_id through
SmartScanStartRequest, WebScanEstimateRequest, DiscoveryMethodRunRequest,
SourceScoutRequest and ad requests, preserving defaults for legacy Beings callers.
Use saved context in AI query scouting, source scouting and repeatable searches;
prioritize relevant approved sources without deleting or excluding other sources.
Company intelligence and applicable paid cohorts use custom mission ranking.
Frontend sends mission_id at estimate/start and seeds editable keyword fields only
when not operator-edited. Record mission identity/revision in durable requests.

## Boundaries
No new compilation/provider calls on mission switch, no scoring changes, no evidence
caps or data purges. Retain original provenance and shared source identities. Budgets,
explicit approvals, freshness, deduplication, and exact inventory totals remain intact.
Generic Web/CT and finite directories retain complete enumeration; query vocabulary
and source/cohort order are the appropriate mission bias, not fabricated size facts.

## Verification
Use synthetic missions and mocked providers for mission terms, weighted criteria,
source ordering, query context, independent cohorts, legacy defaults and round-tripped
requests. Run ruff, targeted pytest and JS syntax/browser checks. No live scan or paid
call. Runtime deployment/rebuild remains explicit; no refresh while editing.

## Risks
Cached/durable request identities must include mission_id to avoid cross-mission
reuse. Do not mutate globally accepted source definitions to customize one execution.
Archived or uncompiled missions must fail clearly, never silently become Beings.

## Implemented and verified
- Saved mission context includes brief, keyword bank, complete criteria/weights/metrics
  and revision. It is passed to existing bounded planners and resolvers, not compiled again.
- Basic/free/AI source queries, standalone Find sources and repeatable source AI use
  that context; approved source order is biased without dropping selected sources.
- Company intelligence and standalone/Basic ad cohorts use existing mission ranking.
- Menu requests and estimates carry mission identity. Editable Basic/ad query defaults
  follow mission changes until edited; reusable goal presets retain mission identity.
- Finite crawls, CT, specialized agency graph collection, shared raw refresh, and generic
  SEO/AI/people collection policies remain unchanged. No evidence/score/freshness changes.
- Ruff and JS syntax pass. Combined verification: 200 tests pass in explicit legacy
  installation mode for the legacy fixtures; browser checks cover keyword seeding,
  manual overrides and reusable presets. New mission tests also pass in public mode.
- Two unrelated pre-existing intelligence tests expect monthly freshness behavior
  inconsistent with the current collection policy; excluded from the final combined run:
  test_dataforseo_monthly_due_filter_is_applied_after_current_rank_cap and
  test_smart_scan_inventory_and_cursor_page_only_monthly_due_work.
- No paid call/live scan, app refresh/restart or Oracle deployment performed. The new
  endpoint and additive fields must deploy with the updated frontend.
