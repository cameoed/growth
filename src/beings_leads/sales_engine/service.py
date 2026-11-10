"""Join buyer evidence, operator judgement and the existing sales ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from ..entity_intelligence import assess_company_role
from ..models import DiscoveredHost, SiteObservation
from .buyer import BuyerFact, build_buyer_assessment
from .buyer_store import BuyerFactStore
from .contacts import recommend_contacts
from .evidence_accounting import decode_record
from .models import (
    CitedEvidence,
    DecisionFeature,
    DecisionSnapshot,
    ProductionOpportunity,
    normalize_company_key,
    validate_source_url,
)
from .persistence import SalesEngineRepository
from .preparation import build_sales_preparation
from .priority import build_sales_priority
from .providers import initialize_visual_schema
from .ranking_modes import stored_priority, validate_mode
from .visual import CreativeAnnotation, cluster_creatives, evaluate_creative_benchmark
from .workload import CreativeWindow, WorkloadEvent, build_workload_intelligence


class SalesEngineService:
    def _public_contacts(self, key, original, now):
        from ..microlink_hydration import public_contacts

        return public_contacts(self.connection, key, original, now=now)

    def __init__(self, store, *, initialize=True):
        self.store = store
        self.connection = store.connection
        self.repo = SalesEngineRepository(self.connection)
        self.buyer_facts = BuyerFactStore(self.connection)
        if not initialize:
            return
        nested_transaction = self.connection.in_transaction
        self.repo.initialize_schema()
        self.buyer_facts.initialize_schema()
        initialize_visual_schema(self.connection)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS engine_workload_inputs(
            id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, kind TEXT NOT NULL,
            payload_json TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
            recorded_at TEXT NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS engine_watchlist(
            company_key TEXT PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            added_at TEXT NOT NULL, next_review_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '')""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS engine_concept_labels(
            company_key TEXT PRIMARY KEY, labels_json TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
        from .ranking import initialize_schema

        initialize_schema(self.connection)
        if not nested_transaction:
            self.connection.commit()

    def _buyer_context(self, candidate_id: int, key: str) -> dict:
        # Candidate links can outlive a redirect or domain correction. Buyer statements
        # belong to the account's company, not every future identity of its candidate.
        row = self.connection.execute(
            "SELECT id FROM sales_accounts WHERE website_host=? "
            "ORDER BY candidate_id=? DESC, id DESC LIMIT 1",
            (normalize_company_key(key), candidate_id),
        ).fetchone()
        return {
            "company_key": key,
            "candidate_id": candidate_id,
            "account": self.store.account_detail(row[0]) if row else None,
            "opportunities": self.repo.list_opportunities(key),
            "reviews": self.repo.list_reviews(key),
        }

    def save_buyer_fact(self, candidate_id: int, fact: BuyerFact) -> dict:
        _, key, _ = self._company(candidate_id)
        if fact.company_key != key:
            raise ValueError("Buyer evidence belongs to a different company")
        return self.buyer_facts.save(fact)

    def _current_target(self, candidate_id: int, target: dict, now: datetime) -> dict:
        from ..desktop_ranking.control import DesktopRankingUnavailable, control_for, submit

        if (control := control_for(self.store)) is not None:
            result = submit(self.store, control, "target", candidate_id=candidate_id,
                            target=target, now=now.isoformat())
            if "target" not in result:
                raise DesktopRankingUnavailable(
                    "Company evidence changed while ranking. Try again."
                )
            return result["target"]
        current = self.store.current_candidate_score(candidate_id, reference_time=now)
        if current is None:
            return target
        scored_row, candidate, observation, score = current
        if target.get("canonical_domain") and normalize_company_key(
            target["canonical_domain"]
        ) != normalize_company_key(observation.canonical_host):
            # Never apply one identity's cached evidence to a different target alias.
            return target
        hypothesis = score.creative_capacity.model_dump(mode="json")
        lane, scope = self.store._target_lane_and_scope(candidate, score)
        excluded = self.connection.execute(
            "SELECT 1 FROM prospect_workflow w WHERE w.excluded=1 AND (w.candidate_id=? "
            "OR w.candidate_id IN (SELECT candidate_id FROM engine_ranking_members "
            "WHERE company_key=?)) LIMIT 1", (candidate_id, observation.canonical_host)
        ).fetchone() is not None
        suppressed = self.connection.execute(
            "SELECT target_is_suppressed(?,?,?)",
            (candidate.source_apex, candidate.hostname, observation.canonical_host),
        ).fetchone()[0]
        from ..frontier_lineage import hiring_boundaries

        boundaries = hiring_boundaries(observation, now)
        microlink_expiry = score.creative_capacity.opportunity.freshness_audit.get(
            "microlink_valid_until")
        if microlink_expiry:
            boundaries.append(datetime.fromisoformat(microlink_expiry))
        latest_id = self.connection.execute(
            "SELECT MAX(id) FROM snapshots WHERE candidate_id=?", (candidate_id,)
        ).fetchone()[0]
        if latest_id != scored_row["snapshot_id"]:
            boundaries.append(observation.observed_at + timedelta(days=90))
        return {
            **target, **hypothesis,
            "canonical_domain": observation.canonical_host,
            "company_role": score.company_role.model_dump(mode="json"),
            "priority_score": score.creative_capacity.opportunity.founder_priority,
            "founder_priority_score": score.creative_capacity.opportunity.founder_priority,
            "disqualified": bool(score.disqualified or excluded or suppressed),
            "target_scope": scope,
            "relationship_lane": lane, "computed_at": now.isoformat(),
            "evidence_valid_until": min(boundaries).isoformat() if boundaries else None,
        }

    def _pipeline_candidate(self, company_key: str) -> int | None:
        row = self.connection.execute(
            "SELECT ti.candidate_id FROM snapshot_target_intelligence ti "
            "WHERE ti.canonical_domain=? AND ti.snapshot_id=(SELECT MAX(s.id) FROM snapshots s "
            "WHERE s.candidate_id=ti.candidate_id) "
            "ORDER BY ti.computed_at DESC, ti.candidate_id DESC LIMIT 1", (company_key,)
        ).fetchone()
        if row:
            _, resolved, _ = self._company(row[0])
            if resolved == company_key:
                return row[0]
        return None

    def pipeline_company(self, account_id: int, *, mode="balanced") -> dict:
        validate_mode(mode)
        account = self.store.account_detail(account_id)
        if account is None:
            raise LookupError("Company record not found")
        key = normalize_company_key(account["website_host"])
        candidate_id = self._pipeline_candidate(key)
        if candidate_id is not None:
            packet = self.packet(candidate_id)
        else:
            packet = {
                "candidate_id": None, "company_key": key, "company_name": account["company"],
                "as_of": datetime.now(UTC).isoformat(), "observed_at": None,
                "role": {}, "target": {}, "public_contacts": {},
                "licensed_professional_contacts": {},
                "opportunities": self.repo.list_opportunities(key),
                "reviews": self.repo.list_reviews(key), "suggested_opportunities": [],
                "buyer_facts": self.buyer_facts.list(key),
            }
        packet["account"] = account
        if "evidence_version" not in packet:
            now = datetime.fromisoformat(packet["as_of"])
            packet["buyer_assessment"] = build_buyer_assessment(
                packet, packet["buyer_facts"], now=now)
            packet["sales_priority"] = build_sales_priority(
                packet["target"] or {}, packet["buyer_assessment"], now=now)
        packet["sales_preparation"] = build_sales_preparation(packet)
        packet["contact_recommendations"] = recommend_contacts(packet)
        packet["related_records"] = [dict(r) for r in self.connection.execute(
            "SELECT id,cohort,lifecycle FROM sales_accounts WHERE website_host=? ORDER BY id DESC",
            (key,)
        )]
        packet["sales_priority"] = stored_priority(self.connection, key, mode,
                                                  packet["sales_priority"], now=datetime.now(UTC))
        return packet

    def company_summary(self, candidate_id: int, *, reference_time=None, mode="balanced"):
        from .summaries import company_summary

        return company_summary(self, candidate_id, reference_time=reference_time, mode=mode)

    def pipeline_summary(self, account_id: int, *, reference_time=None, mode="balanced"):
        from .summaries import company_summary

        return company_summary(self, account_id=account_id,
                               reference_time=reference_time, mode=mode)

    def pipeline(self, *, tag: str = "", search: str = "", stage: str = "", limit: int = 30,
                 offset: int = 0, compact: bool = False, mode="balanced") -> dict:
        validate_mode(mode)
        if compact:
            from .summaries import compact_pipeline

            return compact_pipeline(self, search=search, tag=tag, stage=stage,
                                    limit=limit, offset=offset,
                                    mode=mode)
        from .ranking import company_projection

        limit = max(1, min(limit, 250))
        offset = max(0, offset)
        # One company in the list; legacy cohort records stay accessible without destructive merges.
        query = """WITH companies AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY website_host ORDER BY id DESC) AS n
            FROM sales_accounts
        ) SELECT * FROM companies WHERE n=1
            AND (?='' OR lower(company) LIKE ? OR lower(website_host) LIKE ?)
            AND (?='' OR lifecycle=?)"""
        args = (search, f"%{search.lower()}%", f"%{search.lower()}%", stage, stage)
        total = self.connection.execute("SELECT COUNT(*) FROM (" + query + ")", args).fetchone()[0]
        rows = self.connection.execute(
            query + " ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)
        ).fetchall()
        if mode != "balanced" or tag:
            from .summaries import compact_pipeline

            selected = compact_pipeline(self, search=search, tag=tag, stage=stage, limit=limit,
                                        offset=offset, mode=mode)
            total = selected["total"]
            rows = [self.connection.execute("SELECT * FROM sales_accounts WHERE id=?",
                                           (item["account_id"],)).fetchone()
                    for item in selected["items"]]
        items = []
        now = datetime.now(UTC)
        for row in rows:
            key = row["website_host"]
            projection = company_projection(self.store, key, now=now)
            cid = projection["candidate_id"] if projection else self._pipeline_candidate(key)
            account = self.store.account_detail(row["id"])
            target = projection["target"] if projection else (
                self._current_target(cid, {}, now) if cid is not None else {})
            context = {
                "company_key": key, "candidate_id": cid, "target": target, "account": account,
                "role": target.get("company_role", {}), "reviews": self.repo.list_reviews(key),
                "opportunities": self.repo.list_opportunities(key), "suggested_opportunities": [],
            }
            assessment = projection["assessment"] if projection else build_buyer_assessment(
                context, self.buyer_facts.list(key), now=now)
            items.append({
                "account_id": row["id"], "candidate_id": cid, "company_key": key,
                "company_name": row["company"], "stage": row["lifecycle"],
                "sales_priority": projection["priority"] if projection else build_sales_priority(
                    target, assessment, now=now),
                "as_of": projection["as_of"] if projection else now.isoformat(),
                "evidence_version": projection["evidence_version"] if projection else None,
                "next_action": assessment["next_action"],
            })
            items[-1]["sales_priority"] = stored_priority(
                self.connection, key, mode, items[-1]["sales_priority"], now=now)
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def open_deal(self, candidate_id: int) -> dict:
        candidate, key, _ = self._company(candidate_id)
        existing = self.connection.execute(
            "SELECT id FROM sales_accounts WHERE website_host=? ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()
        if existing:
            return {"account": self.store.account_detail(existing[0]), "created": False}
        detail = self.store.prospect_detail(candidate_id)
        account, created = self.store.create_account(
            candidate_id=candidate_id, company=detail["company_name"], website=f"https://{key}"
        )
        return {"account": account, "created": created}

    def queue(self, *, tag: str = "", search: str = "", limit: int = 30,
              prospects_only: bool = False,
              reference_time: datetime | None = None, compact: bool = False,
              mode="balanced", offset: int = 0) -> dict:
        validate_mode(mode)
        if compact:
            from .summaries import compact_queue

            return compact_queue(self, search=search, tag=tag, limit=limit,
                                 prospects_only=prospects_only,
                                 reference_time=reference_time, mode=mode, offset=offset)
        from .ranking import list_rankings

        result = list_rankings(self.store, search=search, tag=tag, limit=limit,
                               prospects_only=prospects_only, now=reference_time,
                               offset=offset, mode=mode)
        return {**result, **self.tools()}

    def tools(self) -> dict:
        return {
            "watchlist": [dict(row) for row in self.connection.execute(
                "SELECT * FROM engine_watchlist ORDER BY next_review_at LIMIT 100")],
            "budget": self.repo.budget_summary(),
            "learning": self.repo.learning_report(),
        }

    def _company(self, candidate_id: int, *, reference_time: datetime | None = None):
        row = self.connection.execute(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Company not found")
        snapshot_row = self.store.scoring_snapshot(candidate_id, reference_time=reference_time)
        if snapshot_row is None:
            raise LookupError("Company has no observed website yet")
        observations = [SiteObservation.model_validate_json(snapshot_row["observation_json"])]
        candidate = DiscoveredHost(source_apex=row["source_apex"], hostname=row["hostname"])
        key = self.store._target_canonical_domain(candidate, observations[0])
        observations.extend(self.store.time_diverse_snapshot_history(
            candidate_id, before_id=snapshot_row["snapshot_id"], reference_time=reference_time,
            company_key=key))
        # Resolve aliases only in these derived inputs; immutable website snapshots stay intact.
        observations = [
            observation.model_copy(update={"canonical_host": key}) for observation in observations
        ]
        return candidate, normalize_company_key(key), observations

    def annotations(self, company_key: str) -> list[CreativeAnnotation]:
        rows = self.connection.execute(
            """SELECT annotation_json FROM engine_creative_annotations a
            WHERE company_key=? AND id=(SELECT MAX(b.id) FROM engine_creative_annotations b
                WHERE b.company_key=a.company_key AND b.asset_key=a.asset_key)
                ORDER BY id LIMIT 200""",
            (company_key,),
        ).fetchall()
        return [CreativeAnnotation.model_validate_json(row[0]) for row in rows]

    def save_annotation(self, candidate_id: int, annotation: CreativeAnnotation) -> dict:
        _, key, _ = self._company(candidate_id)
        validate_source_url(annotation.source_url)
        now = datetime.now(UTC)
        if annotation.observed_at.tzinfo is None or annotation.observed_at > now:
            raise ValueError("Observation time must be timezone-aware and not in the future")
        if annotation.validated_at and (
            annotation.validated_at.tzinfo is None or annotation.validated_at > now
        ):
            raise ValueError("Review time must be timezone-aware and not in the future")
        serialized = annotation.model_dump_json()
        prior = self.connection.execute(
            """SELECT annotation_json FROM engine_creative_annotations
            WHERE company_key=? AND asset_key=? ORDER BY id DESC LIMIT 1""",
            (key, annotation.asset_key),
        ).fetchone()
        if prior is None or prior[0] != serialized:
            self.connection.execute(
                """INSERT INTO engine_creative_annotations
                (company_key,asset_key,annotation_json,recorded_at) VALUES(?,?,?,?)""",
                (key, annotation.asset_key, serialized, now.isoformat()),
            )
            self.connection.commit()
        return annotation.model_dump(mode="json")

    def add_workload_input(self, candidate_id: int, value: WorkloadEvent | CreativeWindow) -> dict:
        _, key, _ = self._company(candidate_id)
        if normalize_company_key(value.company_key) != key:
            raise ValueError("Evidence belongs to a different company")
        now = datetime.now(UTC)
        if value.observed_at.tzinfo is None or value.observed_at > now:
            raise ValueError("Observation time must be timezone-aware and not in the future")
        if (
            isinstance(value, CreativeWindow)
            and value.window_end
            and (value.window_end.tzinfo is None or value.window_end > value.observed_at)
        ):
            raise ValueError("Census window cannot extend beyond observation time")
        urls = [value.source_url] if isinstance(value, WorkloadEvent) else value.source_urls
        for url in urls:
            validate_source_url(url)
        if isinstance(value, WorkloadEvent) and not value.deliverables:
            from ..hiring_intelligence import extract_deliverable_expectations

            value = value.model_copy(
                update={
                    "deliverables": [
                        asdict(item) for item in extract_deliverable_expectations(value.excerpt)
                    ]
                }
            )
        serialized = value.model_dump_json()
        kind = "event" if isinstance(value, WorkloadEvent) else "window"
        self.connection.execute(
            """INSERT OR IGNORE INTO engine_workload_inputs
            (company_key,kind,payload_json,fingerprint,recorded_at) VALUES(?,?,?,?,?)""",
            (
                key,
                kind,
                serialized,
                hashlib.sha256(serialized.encode()).hexdigest(),
                now.isoformat(),
            ),
        )
        self.connection.commit()
        return value.model_dump(mode="json")

    def save_concept_labels(self, candidate_id: int, labels: dict[str, str]) -> dict:
        _, key, _ = self._company(candidate_id)
        annotations = self.annotations(key)
        keys = {a.asset_key for a in annotations}
        if any(
            asset not in keys or not label.strip() or len(label) > 200
            for asset, label in labels.items()
        ):
            raise ValueError("Labels must identify existing assets and use 1–200 characters")
        result = evaluate_creative_benchmark(annotations, labels)
        self.connection.execute(
            """INSERT INTO engine_concept_labels VALUES(?,?,?)
            ON CONFLICT(company_key) DO UPDATE SET labels_json=excluded.labels_json,
            recorded_at=excluded.recorded_at""",
            (key, json.dumps(labels), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return result.model_dump(mode="json")

    def packet(self, candidate_id: int, *, reference_time: datetime | None = None,
               mode="balanced") -> dict:
        validate_mode(mode)
        from .ranking import company_projection

        now = reference_time or datetime.now(UTC)
        candidate, key, observations = self._company(candidate_id, reference_time=now)
        projection = company_projection(self.store, key, now=now)
        if projection is not None:
            candidate_id = projection["candidate_id"]
            candidate, key, observations = self._company(candidate_id, reference_time=now)
            now = datetime.fromisoformat(projection["as_of"])
        current = observations[0].model_copy(deep=True)
        current.web.frontier = self.store.frontier_intelligence(key)
        inputs = self.connection.execute(
            """SELECT kind,payload_json FROM engine_workload_inputs
            WHERE company_key=? ORDER BY id DESC LIMIT 200""",
            (key,),
        ).fetchall()
        events = [WorkloadEvent.model_validate_json(row[1]) for row in inputs if row[0] == "event"]
        windows = [
            CreativeWindow.model_validate_json(row[1]) for row in inputs if row[0] == "window"
        ]
        # Public ad censuses are partial unless the collector explicitly recorded completeness.
        ad_rows = self.connection.execute(
            """SELECT o.*, r.request_json FROM candidate_ad_observations o
            JOIN ad_discovery_runs r ON r.id=o.run_id WHERE o.candidate_id=?
            AND r.status IN ('complete','partial') ORDER BY o.observed_at DESC LIMIT 24""",
            (candidate_id,),
        ).fetchall()
        for row in ad_rows:
            metadata = (
                json.loads(row["metadata_json"] or "{}")
                if "metadata_json" in set(row.keys())
                else {}
            )
            if metadata.get("manual_mapping_review"):
                continue
            request = json.loads(row["request_json"] or "{}")
            scope = json.dumps(
                {k: request.get(k) for k in ("country", "active_only", "mode")}, sort_keys=True
            )
            windows.append(
                CreativeWindow(
                    company_key=key,
                    scope=f"ads:{row['platform']}:{row['surface']}:{scope}",
                    observed_at=row["observed_at"],
                    identity_verified=(
                        row["identity_state"] == "verified"
                        and int(row["identity_confidence"]) >= 80
                    ),
                    coverage="partial",  # Current collectors are capped, never exhaustive censuses.
                    asset_keys=json.loads(row["creative_group_ids_json"] or "[]")[:500],
                    source_urls=json.loads(row["evidence_urls_json"] or "[]")[:30],
                    channel=row["platform"],
                    limitations=json.loads(row["coverage_notes_json"] or "[]")[:20],
                )
            )
        annotations = self.annotations(key)
        workload = build_workload_intelligence(
            current,
            observations[1:],
            events=events,
            ads=windows,
            annotations=annotations,
            now=now,
        )
        suggested = []
        for task in workload.tasks:
            evidence = [
                CitedEvidence(
                    source_url=e.source_url,
                    excerpt=e.excerpt,
                    observed_at=e.observed_at,
                    subject_company_key=e.company_key,
                    evidence_kind=e.evidence_kind,
                )
                for e in task.evidence
                if e.excerpt
            ]
            if not evidence:
                continue
            opportunity = ProductionOpportunity(
                id="work_" + task.task_key,
                company_key=key,
                candidate_id=candidate_id,
                title=task.title,
                description=task.description,
                evidence=evidence,
                required_skills=task.required_skills,
                channel_tags=task.channel_tags,
                languages=task.languages,
                missing_inputs=task.missing_inputs,
                counter_hypotheses=task.counter_hypotheses,
                observed_at=max(item.observed_at for item in evidence),
                # A generic planning range is not a buyer-confirmed workload.
                estimated_hours=None,
            )
            suggested.append(
                {
                    "opportunity": opportunity.model_dump(mode="json"),
                    "planning_range": [task.estimated_hours_min, task.estimated_hours_max],
                    "effort_basis": task.effort_basis,
                }
            )
        detail = self.store.prospect_detail(candidate_id, include_ad_intelligence=True)
        buyer_context = self._buyer_context(candidate_id, key)
        account = buyer_context["account"]
        reviews = buyer_context["reviews"]
        relationships = self.repo.list_relationships(key)
        observed_relationships = [
            dict(row)
            for row in self.connection.execute(
                """SELECT
            subject_key, relationship_type, object_key, object_name, source_url, confidence,
            first_observed_at, last_observed_at FROM company_relationships
            WHERE subject_key IN (?,?) OR object_key IN (?,?)
            ORDER BY last_observed_at DESC LIMIT 30""",
                (key, f"domain:{key}", key, f"domain:{key}"),
            ).fetchall()
        ]
        labels = self.connection.execute(
            "SELECT labels_json FROM engine_concept_labels WHERE company_key=?", (key,)
        ).fetchone()
        benchmark = (
            evaluate_creative_benchmark(annotations, json.loads(labels[0])) if labels else None
        )
        from ..buyer_intelligence import load_evidence

        packet = {
            "buyer_evidence": load_evidence(self.connection, key, now=now),
            "candidate_id": candidate_id,
            "company_key": key,
            "company_name": detail["company_name"],
            "as_of": workload.as_of.isoformat(),
            "observed_at": current.observed_at.isoformat(),
            "role": assess_company_role(current, candidate=candidate).model_dump(mode="json"),
            "target": self._current_target(
                candidate_id, detail.get("target_intelligence") or {}, workload.as_of
            ),
            "workload": {
                **workload.model_dump(mode="json"),
                "source_events": [event.model_dump(mode="json") for event in events],
            },
            "suggested_opportunities": suggested,
            "opportunities": buyer_context["opportunities"],
            "relationships": relationships,
            "observed_relationships": observed_relationships,
            "public_contacts": self._public_contacts(key, detail["contacts"], now),
            "licensed_professional_contacts": detail.get("people_intelligence", {}),
            "reviews": reviews,
            "account": account,
            "creative": cluster_creatives(annotations, benchmark=benchmark).model_dump(mode="json"),
            "benchmark": benchmark.model_dump(mode="json") if benchmark else None,
            "annotations": [a.model_dump(mode="json") for a in annotations],
            "budget": self.repo.budget_summary(company_key=key),
        }

        packet["buyer_facts"] = self.buyer_facts.list(key)
        packet["buyer_assessment"] = build_buyer_assessment(
            packet,
            packet["buyer_facts"],
            now=now,
        )
        packet["next_action"] = packet["buyer_assessment"]["next_action"]
        packet["unknowns"] = packet["buyer_assessment"]["unknowns"]
        packet["sales_preparation"] = build_sales_preparation(packet)
        packet["sales_priority"] = build_sales_priority(
            packet["target"] or {}, packet["buyer_assessment"], now=now
        )
        if projection is not None:
            packet["target"] = projection["target"]
            packet["buyer_assessment"] = projection["assessment"]
            packet["sales_priority"] = projection["priority"]
            packet["policy_version"] = projection["policy_version"]
            packet["evidence_version"] = projection["evidence_version"]
            packet["evidence_accounting"] = [
                json.loads(decode_record(row[0])) for row in self.connection.execute(
                    "SELECT record_json FROM engine_evidence_accounting WHERE company_key=? "
                    "AND as_of=? AND policy_version=? AND evidence_version=? ORDER BY input_key",
                    (key, projection["as_of"], projection["policy_version"],
                     projection["evidence_version"]),
                )
            ]
        packet["ranking_pending"] = projection is None
        packet["contact_recommendations"] = recommend_contacts(packet)
        packet["sales_priority"] = stored_priority(
            self.connection, key, mode, packet["sales_priority"],
            now=reference_time or datetime.now(UTC))
        return packet

    def record_decision(self, candidate_id: int, idempotency_key: str) -> dict:
        prior = self.connection.execute(
            "SELECT * FROM engine_decisions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior:
            saved = json.loads(prior["payload_json"])
            if saved.get("candidate_id") != candidate_id:
                raise ValueError("Decision key belongs to a different company")
            # Replay the original event even if a referenced account or candidate has
            # since changed identity. New decisions still validate current references.
            return self.repo._event(prior)
        packet = self.packet(candidate_id)
        key = packet["company_key"]
        now = datetime.now(UTC)
        observed_at = datetime.fromisoformat(packet["observed_at"])
        task_observed_at = max(
            (
                datetime.fromisoformat(e["observed_at"])
                for task in packet["suggested_opportunities"]
                for e in task["opportunity"]["evidence"]
            ),
            default=observed_at,
        )
        return self.repo.record_decision(
            DecisionSnapshot(
                idempotency_key=idempotency_key,
                company_key=key,
                candidate_id=candidate_id,
                account_id=(packet["account"] or {}).get("id"),
                as_of=now,
                action=packet["next_action"]["action"],
                rationale=packet["next_action"]["question"],
                next_research_action=packet["next_action"]["research_action"],
                policy_version="sales-engine-priority-v3",
                review_ids=packet["buyer_assessment"]["controlling_review_ids"],
                features=[
                    DecisionFeature(
                        key="role", value=packet["role"]["role"], observed_at=observed_at
                    ),
                    DecisionFeature(
                        key="priority",
                        value=packet["sales_priority"]["score"],
                        observed_at=observed_at,
                    ),
                    DecisionFeature(
                        key="sales_priority_version",
                        value=packet["sales_priority"]["version"],
                        observed_at=now,
                    ),
                    *[
                        DecisionFeature(
                            key="buyer." + criterion["key"],
                            value=criterion["state"],
                            observed_at=datetime.fromisoformat(criterion["observed_at"])
                            if criterion.get("observed_at")
                            else now,
                        )
                        for criterion in packet["buyer_assessment"]["criteria"]
                    ],
                    DecisionFeature(
                        key="buyer_fact_ids",
                        value=json.dumps(packet["buyer_assessment"]["fact_ids"]),
                        observed_at=now,
                    ),
                    DecisionFeature(
                        key="specific_tasks",
                        value=len(packet["suggested_opportunities"]),
                        observed_at=task_observed_at,
                    ),
                ],
            )
        )

    def watch(self, candidate_id: int, *, active: bool = True) -> dict:
        _, key, _ = self._company(candidate_id)
        now = datetime.now(UTC)
        if active:
            count = self.connection.execute("SELECT COUNT(*) FROM engine_watchlist").fetchone()[0]
            if (
                count >= 100
                and not self.connection.execute(
                    "SELECT 1 FROM engine_watchlist WHERE company_key=?", (key,)
                ).fetchone()
            ):
                raise ValueError("Pilot watchlist is capped at 100 companies")
            self.connection.execute(
                """INSERT INTO engine_watchlist VALUES(?,?,?,?,?)
                ON CONFLICT(company_key) DO UPDATE SET next_review_at=excluded.next_review_at""",
                (key, candidate_id, now.isoformat(), (now + timedelta(days=7)).isoformat(), ""),
            )
        else:
            self.connection.execute("DELETE FROM engine_watchlist WHERE company_key=?", (key,))
        self.connection.commit()
        return {"company_key": key, "active": active, "provider_requests": 0}
