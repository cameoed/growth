"""Additive SQLite persistence for the local sales workbench.

The caller owns the connection. Independent writes commit; writes inside an existing
transaction use a savepoint and never commit the caller's work. Budget reservations
take the SQLite writer lock before reading totals, including across connections.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .models import (
    BudgetPolicy,
    BudgetReservationRequest,
    DecisionSnapshot,
    EngineModel,
    ProductionOpportunity,
    RelationshipEvidence,
    ResearchReview,
    normalize_company_key,
    utc_now,
)


def _json(value: Any) -> str:
    if isinstance(value, EngineModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _timestamp(value: datetime | None = None) -> str:
    stamp = value or utc_now()
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.astimezone(UTC).isoformat()


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS engine_opportunities (
        id TEXT PRIMARY KEY, company_key TEXT NOT NULL, candidate_id INTEGER, account_id INTEGER,
        status TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_opportunities_company
        ON engine_opportunities(company_key, status, updated_at)""",
    """CREATE TABLE IF NOT EXISTS engine_relationships (
        id TEXT PRIMARY KEY, subject_company_key TEXT NOT NULL, object_company_key TEXT NOT NULL,
        relationship_type TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_relationships_subject
        ON engine_relationships(subject_company_key)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_relationships_object
        ON engine_relationships(object_company_key)""",
    """CREATE TABLE IF NOT EXISTS engine_research_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
        company_key TEXT NOT NULL, candidate_id INTEGER, account_id INTEGER,
        observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_reviews_company
        ON engine_research_reviews(company_key, recorded_at)""",
    """CREATE TABLE IF NOT EXISTS engine_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
        company_key TEXT NOT NULL, candidate_id INTEGER, account_id INTEGER,
        policy_version TEXT NOT NULL, as_of TEXT NOT NULL, recorded_at TEXT NOT NULL,
        experiment_arm TEXT, payload_json TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_decisions_company
        ON engine_decisions(company_key, as_of, recorded_at)""",
    """CREATE TABLE IF NOT EXISTS engine_experiments (
        company_key TEXT NOT NULL, experiment_key TEXT NOT NULL,
        exploration_percent INTEGER NOT NULL CHECK(exploration_percent BETWEEN 0 AND 100),
        arm TEXT NOT NULL CHECK(arm IN ('baseline', 'exploration')), assigned_at TEXT NOT NULL,
        PRIMARY KEY(company_key, experiment_key))""",
    """CREATE TABLE IF NOT EXISTS engine_account_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        company_key TEXT NOT NULL, candidate_id INTEGER, state_hash TEXT NOT NULL,
        recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_account_states_time
        ON engine_account_states(account_id, recorded_at, id)""",
    """CREATE TABLE IF NOT EXISTS engine_budget_policy (
        id INTEGER PRIMARY KEY CHECK(id=1),
        monthly_limit_microusd INTEGER NOT NULL CHECK(monthly_limit_microusd >= 0),
        per_account_limit_microusd INTEGER NOT NULL CHECK(per_account_limit_microusd >= 0),
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS engine_budget_reservations (
        idempotency_key TEXT PRIMARY KEY, company_key TEXT NOT NULL, month TEXT NOT NULL,
        provider TEXT NOT NULL, purpose TEXT NOT NULL,
        maximum_microusd INTEGER NOT NULL CHECK(maximum_microusd > 0),
        actual_microusd INTEGER CHECK(actual_microusd >= 0 AND actual_microusd <= maximum_microusd),
        status TEXT NOT NULL CHECK(status IN ('reserved', 'settled', 'cancelled')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((status='reserved' AND actual_microusd IS NULL)
           OR (status='settled' AND actual_microusd IS NOT NULL)
           OR (status='cancelled' AND actual_microusd=0)))""",
    """CREATE TABLE IF NOT EXISTS engine_account_budget_limits (
        company_key TEXT PRIMARY KEY, limit_microusd INTEGER NOT NULL CHECK(limit_microusd >= 0),
        updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_engine_budget_month
        ON engine_budget_reservations(month, company_key, status)""",
)


class SalesEngineRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        nested = self.connection.in_transaction
        savepoint = f"engine_{uuid4().hex}"
        try:
            self.connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            yield
            if nested:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.commit()
        except BaseException:
            if nested:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.connection.rollback()
            raise

    def initialize_schema(self) -> None:
        """Idempotent schema creation only: no account, observation or customer backfill."""
        from .ranking import initialize_schema

        required = {
            re.search(r"CREATE (?:TABLE|INDEX) IF NOT EXISTS (\w+)", statement)[1]
            for statement in SCHEMA
        }
        installed = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master")}
        if required <= installed and self.connection.execute(
            "SELECT 1 FROM engine_budget_policy WHERE id=1"
        ).fetchone():
            # Existing WAL readers must not take a writer lock merely to open the queue
            # while collection or the inventory backfill is committing new evidence.
            initialize_schema(self.connection)
            return
        with self._transaction():
            for statement in SCHEMA:
                self.connection.execute(statement)
            default = BudgetPolicy()
            self.connection.execute(
                """INSERT OR IGNORE INTO engine_budget_policy VALUES(1,?,?,?)""",
                (default.monthly_limit_microusd, default.per_account_limit_microusd, _timestamp()),
            )

            initialize_schema(self.connection)

    def _has_table(self, table: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def _validate_refs(
        self,
        company_key: str,
        candidate_id: int | None,
        account_id: int | None,
    ) -> None:
        if candidate_id is not None:
            candidate = (
                self.connection.execute(
                    "SELECT hostname FROM candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                if self._has_table("candidates")
                else None
            )
            if candidate is None:
                raise ValueError("candidate_id does not exist")
            accepted_keys = {normalize_company_key(candidate["hostname"])}
            if self._has_table("snapshot_target_intelligence"):
                projection = self.connection.execute(
                    """SELECT canonical_domain FROM snapshot_target_intelligence
                    WHERE candidate_id=? ORDER BY snapshot_id DESC LIMIT 1""",
                    (candidate_id,),
                ).fetchone()
                if projection:
                    # An invalid projection cannot authorize another company identity.
                    with suppress(ValueError, AttributeError):
                        accepted_keys.add(normalize_company_key(projection["canonical_domain"]))
            if company_key not in accepted_keys:
                raise ValueError("candidate_id belongs to a different company")
        if account_id is not None:
            account = (
                self.connection.execute(
                    "SELECT candidate_id,website_host,website FROM sales_accounts WHERE id=?",
                    (account_id,),
                ).fetchone()
                if self._has_table("sales_accounts")
                else None
            )
            if account is None:
                raise ValueError("account_id does not exist")
            account_key = normalize_company_key(account["website_host"] or account["website"])
            if account_key != company_key:
                raise ValueError("account_id belongs to a different company")
            if candidate_id is not None and account["candidate_id"] not in {None, candidate_id}:
                raise ValueError("account_id and candidate_id are inconsistent")

    @staticmethod
    def _entity(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **json.loads(row["payload_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_opportunity(self, opportunity: ProductionOpportunity) -> dict[str, Any]:
        opportunity = ProductionOpportunity.model_validate(opportunity.model_dump())
        # Legacy payloads remain readable. New writes must preserve who heard the buyer's
        # confirmation and when; a public page marked human_confirmed cannot stand in for it.
        if opportunity.status == "confirmed" and not any(
            item.subject_company_key == opportunity.company_key
            and item.evidence_kind == "human_confirmed"
            and item.source_kind == "buyer_conversation"
            and item.recorded_by
            for item in opportunity.evidence
        ):
            raise ValueError(
                "confirmed opportunities require company-attributed buyer-conversation "
                "evidence with a recorder and the actual observation date"
            )
        self._validate_refs(
            opportunity.company_key, opportunity.candidate_id, opportunity.account_id
        )
        now = utc_now()
        if opportunity.observed_at > now or any(
            item.observed_at > now for item in opportunity.evidence
        ):
            raise ValueError("opportunity evidence cannot be observed in the future")
        key = (
            opportunity.id
            or "opp_"
            + _hash(
                {
                    "company": opportunity.company_key,
                    "title": opportunity.title,
                }
            )[:24]
        )
        opportunity = opportunity.model_copy(update={"id": key})
        stamp = _timestamp(now)
        with self._transaction():
            prior = self.connection.execute(
                "SELECT company_key FROM engine_opportunities WHERE id=?", (key,)
            ).fetchone()
            if prior and prior["company_key"] != opportunity.company_key:
                raise ValueError("an opportunity cannot move to another company")
            self.connection.execute(
                """INSERT INTO engine_opportunities VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET candidate_id=excluded.candidate_id,
                    account_id=excluded.account_id,status=excluded.status,
                    payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (
                    key,
                    opportunity.company_key,
                    opportunity.candidate_id,
                    opportunity.account_id,
                    opportunity.status,
                    _json(opportunity),
                    stamp,
                    stamp,
                ),
            )
        return self._entity(
            self.connection.execute(
                "SELECT * FROM engine_opportunities WHERE id=?", (key,)
            ).fetchone()
        )

    def list_opportunities(self, company_key: str | None = None) -> list[dict[str, Any]]:
        key = normalize_company_key(company_key) if company_key else None
        return [
            self._entity(row)
            for row in self.connection.execute(
                """SELECT * FROM engine_opportunities WHERE (? IS NULL OR company_key=?)
            ORDER BY updated_at DESC,id""",
                (key, key),
            ).fetchall()
        ]

    def save_relationship(self, relationship: RelationshipEvidence) -> dict[str, Any]:
        relationship = RelationshipEvidence.model_validate(relationship.model_dump())
        now = utc_now()
        if relationship.observed_at > now or (
            relationship.permission_recorded_at and relationship.permission_recorded_at > now
        ):
            raise ValueError("relationship evidence and permission cannot be future-dated")
        key = (
            relationship.id
            or "rel_"
            + _hash(
                [
                    relationship.subject_company_key,
                    relationship.object_company_key,
                    relationship.relationship_type,
                    relationship.source_url,
                ]
            )[:24]
        )
        relationship = relationship.model_copy(update={"id": key})
        stamp = _timestamp(now)
        with self._transaction():
            self.connection.execute(
                """INSERT INTO engine_relationships VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET subject_company_key=excluded.subject_company_key,
                    object_company_key=excluded.object_company_key,
                    relationship_type=excluded.relationship_type,
                    payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (
                    key,
                    relationship.subject_company_key,
                    relationship.object_company_key,
                    relationship.relationship_type,
                    _json(relationship),
                    stamp,
                    stamp,
                ),
            )
        return self._entity(
            self.connection.execute(
                "SELECT * FROM engine_relationships WHERE id=?", (key,)
            ).fetchone()
        )

    def list_relationships(self, company_key: str | None = None) -> list[dict[str, Any]]:
        key = normalize_company_key(company_key) if company_key else None
        return [
            self._entity(row)
            for row in self.connection.execute(
                """SELECT * FROM engine_relationships WHERE
            (? IS NULL OR subject_company_key=? OR object_company_key=?)
            ORDER BY updated_at DESC,id""",
                (key, key, key),
            ).fetchall()
        ]

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        payload = {
            **json.loads(row["payload_json"]),
            "id": row["id"],
            "recorded_at": row["recorded_at"],
        }
        if "experiment_arm" in dict(row):
            payload["experiment_arm"] = row["experiment_arm"]
        return payload

    def add_review(self, review: ResearchReview) -> dict[str, Any]:
        review = ResearchReview.model_validate(review.model_dump())
        self._validate_refs(review.company_key, review.candidate_id, review.account_id)
        if review.observed_at > utc_now():
            raise ValueError("review cannot be observed in the future")
        serialized = _json(review)
        with self._transaction():
            prior = self.connection.execute(
                "SELECT * FROM engine_research_reviews WHERE idempotency_key=?",
                (review.idempotency_key,),
            ).fetchone()
            if prior:
                if prior["payload_json"] != serialized:
                    raise ValueError("idempotency key is already used for a different review")
                return self._event(prior)
            self.connection.execute(
                """INSERT INTO engine_research_reviews(idempotency_key,company_key,candidate_id,
                account_id,observed_at,recorded_at,payload_json) VALUES(?,?,?,?,?,?,?)""",
                (
                    review.idempotency_key,
                    review.company_key,
                    review.candidate_id,
                    review.account_id,
                    _timestamp(review.observed_at),
                    _timestamp(),
                    serialized,
                ),
            )
        return self._event(
            self.connection.execute(
                "SELECT * FROM engine_research_reviews WHERE idempotency_key=?",
                (review.idempotency_key,),
            ).fetchone()
        )

    def list_reviews(self, company_key: str | None = None) -> list[dict[str, Any]]:
        key = normalize_company_key(company_key) if company_key else None
        return [
            self._event(row)
            for row in self.connection.execute(
                """SELECT * FROM engine_research_reviews WHERE (? IS NULL OR company_key=?)
            ORDER BY recorded_at DESC,id DESC""",
                (key, key),
            ).fetchall()
        ]

    def assign_experiment(
        self,
        company_key: str,
        experiment_key: str,
        exploration_percent: int = 10,
    ) -> dict[str, Any]:
        company_key = normalize_company_key(company_key)
        if not re.fullmatch(r"[\w.:-]{1,120}", experiment_key):
            raise ValueError("experiment_key must be a nonempty identifier")
        if type(exploration_percent) is not int or not 0 <= exploration_percent <= 100:
            raise ValueError("exploration_percent must be an integer from 0 through 100")
        with self._transaction():
            prior = self.connection.execute(
                "SELECT * FROM engine_experiments WHERE company_key=? AND experiment_key=?",
                (company_key, experiment_key),
            ).fetchone()
            if prior:
                if prior["exploration_percent"] != exploration_percent:
                    raise ValueError("use a new experiment key to change allocation")
                return dict(prior)
            bucket = int(_hash([experiment_key, company_key])[:16], 16) % 100
            arm = "exploration" if bucket < exploration_percent else "baseline"
            self.connection.execute(
                "INSERT INTO engine_experiments VALUES(?,?,?,?,?)",
                (
                    company_key,
                    experiment_key,
                    exploration_percent,
                    arm,
                    _timestamp(),
                ),
            )
        return dict(
            self.connection.execute(
                "SELECT * FROM engine_experiments WHERE company_key=? AND experiment_key=?",
                (company_key, experiment_key),
            ).fetchone()
        )

    def record_decision(self, decision: DecisionSnapshot) -> dict[str, Any]:
        decision = DecisionSnapshot.model_validate(decision.model_dump())
        self._validate_refs(decision.company_key, decision.candidate_id, decision.account_id)
        if decision.as_of > utc_now():
            raise ValueError("decision cannot be made as of a future time")
        stamp = _timestamp(decision.as_of)
        serialized = _json(decision)
        with self._transaction():
            prior = self.connection.execute(
                "SELECT * FROM engine_decisions WHERE idempotency_key=?",
                (decision.idempotency_key,),
            ).fetchone()
            if prior:
                if prior["payload_json"] != serialized:
                    raise ValueError("idempotency key is already used for a different decision")
                return self._event(prior)
            for review_id in decision.review_ids:
                row = self.connection.execute(
                    """SELECT company_key,observed_at,recorded_at FROM engine_research_reviews
                    WHERE id=?""",
                    (review_id,),
                ).fetchone()
                if row is None or row["company_key"] != decision.company_key:
                    raise ValueError("decision review must belong to the same company")
                if row["observed_at"] > stamp or row["recorded_at"] > stamp:
                    raise ValueError("decision cannot use a review recorded after as_of")
            arm = None
            if decision.experiment_key:
                experiment = self.connection.execute(
                    """SELECT arm,assigned_at FROM engine_experiments
                    WHERE company_key=? AND experiment_key=?""",
                    (decision.company_key, decision.experiment_key),
                ).fetchone()
                if not experiment or experiment["assigned_at"] > stamp:
                    raise ValueError("experiment assignment must exist before the decision")
                arm = experiment["arm"]
            self.connection.execute(
                """INSERT INTO engine_decisions(idempotency_key,company_key,candidate_id,account_id,
                policy_version,as_of,recorded_at,experiment_arm,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    decision.idempotency_key,
                    decision.company_key,
                    decision.candidate_id,
                    decision.account_id,
                    decision.policy_version,
                    stamp,
                    _timestamp(),
                    arm,
                    serialized,
                ),
            )
        return self._event(
            self.connection.execute(
                "SELECT * FROM engine_decisions WHERE idempotency_key=?",
                (decision.idempotency_key,),
            ).fetchone()
        )

    def list_decisions(self, company_key: str | None = None) -> list[dict[str, Any]]:
        key = normalize_company_key(company_key) if company_key else None
        return [
            self._event(row)
            for row in self.connection.execute(
                """SELECT * FROM engine_decisions WHERE (? IS NULL OR company_key=?)
            ORDER BY as_of DESC,id DESC""",
                (key, key),
            ).fetchall()
        ]

    def capture_account_state(self, account_id: int) -> dict[str, Any]:
        if not self._has_table("sales_accounts"):
            raise ValueError("sales accounts are unavailable")
        account = self.connection.execute(
            "SELECT * FROM sales_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if account is None:
            raise ValueError("account_id does not exist")
        payload = dict(account)
        company_key = normalize_company_key(payload.get("website_host") or payload["website"])
        # A repeated save can advance the mutable row timestamp without introducing
        # any new commercial fact. Retain the original recorded_at and payload for
        # that state so neither duplicate history nor hindsight is manufactured.
        digest = _hash({key: value for key, value in payload.items() if key != "updated_at"})
        with self._transaction():
            last = self.connection.execute(
                """SELECT * FROM engine_account_states
                WHERE account_id=? ORDER BY id DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
            last_semantic_hash = None
            if last:
                last_payload = json.loads(last["payload_json"])
                last_semantic_hash = _hash(
                    {key: value for key, value in last_payload.items() if key != "updated_at"}
                )
            if last and last_semantic_hash == digest:
                return {
                    "id": last["id"],
                    "account_id": account_id,
                    "recorded_at": last["recorded_at"],
                }
            cursor = self.connection.execute(
                """INSERT INTO engine_account_states(account_id,company_key,candidate_id,state_hash,
                recorded_at,payload_json) VALUES(?,?,?,?,?,?)""",
                (
                    account_id,
                    company_key,
                    payload.get("candidate_id"),
                    digest,
                    _timestamp(),
                    _json(payload),
                ),
            )
            return {
                "id": cursor.lastrowid,
                "account_id": account_id,
                "recorded_at": self.connection.execute(
                    "SELECT recorded_at FROM engine_account_states WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()["recorded_at"],
            }

    @contextmanager
    def _budget_transaction(self) -> Iterator[None]:
        with self._transaction():
            # Acquire the writer lock before totals even in a caller's deferred transaction.
            self.connection.execute("UPDATE engine_budget_policy SET id=id WHERE id=1")
            yield

    def configure_budget(self, policy: BudgetPolicy) -> dict[str, Any]:
        policy = BudgetPolicy.model_validate(policy.model_dump())
        with self._budget_transaction():
            summary = self.budget_summary()
            if policy.monthly_limit_microusd < summary["committed_microusd"]:
                raise ValueError("monthly limit cannot be below current committed spend")
            if any(
                account["committed_microusd"] > policy.per_account_limit_microusd
                and not account["has_limit_override"]
                for account in summary["accounts"]
            ):
                raise ValueError(
                    "account limit cannot be below an account's current committed spend"
                )
            self.connection.execute(
                """UPDATE engine_budget_policy SET monthly_limit_microusd=?,
                per_account_limit_microusd=?,updated_at=? WHERE id=1""",
                (policy.monthly_limit_microusd, policy.per_account_limit_microusd, _timestamp()),
            )
        return self.budget_summary()

    def configure_account_budget(self, company_key: str, limit_microusd: int) -> dict[str, Any]:
        company_key = normalize_company_key(company_key)
        validated = BudgetPolicy(per_account_limit_microusd=limit_microusd)
        with self._budget_transaction():
            summary = self.budget_summary(company_key=company_key)
            if validated.per_account_limit_microusd < summary["account"]["committed_microusd"]:
                raise ValueError("account limit cannot be below current committed spend")
            self.connection.execute(
                """INSERT INTO engine_account_budget_limits VALUES(?,?,?)
                ON CONFLICT(company_key) DO UPDATE SET limit_microusd=excluded.limit_microusd,
                updated_at=excluded.updated_at""",
                (company_key, limit_microusd, _timestamp()),
            )
        return self.budget_summary(company_key=company_key)

    def budget_summary(
        self,
        month: str | None = None,
        company_key: str | None = None,
    ) -> dict[str, Any]:
        month = month or utc_now().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("month must be YYYY-MM")
        key = normalize_company_key(company_key) if company_key else None
        policy = dict(
            self.connection.execute("SELECT * FROM engine_budget_policy WHERE id=1").fetchone()
        )
        rows = self.connection.execute(
            """SELECT company_key,
                SUM(CASE WHEN status='reserved' THEN maximum_microusd ELSE 0 END) reserved_microusd,
                SUM(CASE WHEN status='settled' THEN actual_microusd ELSE 0 END) spent_microusd
            FROM engine_budget_reservations WHERE month=?
            GROUP BY company_key ORDER BY company_key""",
            (month,),
        ).fetchall()
        # Provider overruns are actual incurred debits, not an expanded authorization. Keep
        # original reservation ceilings intact and include independently recorded excess costs.
        if self._has_table("buyer_collection_receipts"):
            overruns = {row["company_key"]: row["excess"] for row in self.connection.execute(
                "SELECT company_key,SUM(actual_microusd-maximum_microusd) AS excess "
                "FROM buyer_collection_receipts WHERE actual_microusd>maximum_microusd "
                "AND substr(observed_at,1,7)=? GROUP BY company_key", (month,))}
            rows = [{**dict(row), "spent_microusd": row["spent_microusd"]
                     + overruns.pop(row["company_key"], 0)} for row in rows]
            rows.extend({"company_key": company, "reserved_microusd": 0,
                         "spent_microusd": excess} for company, excess in overruns.items())
        overrides = {
            row["company_key"]: row["limit_microusd"]
            for row in self.connection.execute(
                "SELECT company_key,limit_microusd FROM engine_account_budget_limits"
            ).fetchall()
        }
        accounts = [
            {
                **dict(row),
                "committed_microusd": row["reserved_microusd"] + row["spent_microusd"],
                "limit_microusd": overrides.get(
                    row["company_key"], policy["per_account_limit_microusd"]
                ),
                "has_limit_override": row["company_key"] in overrides,
                "remaining_microusd": max(
                    0,
                    overrides.get(row["company_key"], policy["per_account_limit_microusd"])
                    - row["reserved_microusd"]
                    - row["spent_microusd"],
                ),
            }
            for row in rows
        ]
        reserved = sum(row["reserved_microusd"] for row in rows)
        spent = sum(row["spent_microusd"] for row in rows)
        selected = next((item for item in accounts if item["company_key"] == key), None)
        if key and selected is None:
            selected = {
                "company_key": key,
                "reserved_microusd": 0,
                "spent_microusd": 0,
                "committed_microusd": 0,
                "limit_microusd": overrides.get(key, policy["per_account_limit_microusd"]),
                "has_limit_override": key in overrides,
                "remaining_microusd": overrides.get(key, policy["per_account_limit_microusd"]),
            }
        return {
            "month": month,
            "currency": "USD",
            "unit": "microUSD",
            "monthly_limit_microusd": policy["monthly_limit_microusd"],
            "per_account_limit_microusd": policy["per_account_limit_microusd"],
            "reserved_microusd": reserved,
            "spent_microusd": spent,
            "committed_microusd": reserved + spent,
            "remaining_microusd": max(0, policy["monthly_limit_microusd"] - reserved - spent),
            "accounts": accounts,
            "account": selected,
            "account_limit_overrides": overrides,
        }

    def reserve_budget(self, request: BudgetReservationRequest) -> dict[str, Any]:
        request = BudgetReservationRequest.model_validate(request.model_dump())
        with self._budget_transaction():
            existing = self.connection.execute(
                "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if existing:
                if any(existing[key] != value for key, value in request.model_dump().items()):
                    raise ValueError("idempotency key is already used for another reservation")
                return {**dict(existing), "created": False}
            stamp = _timestamp()
            month = stamp[:7]
            summary = self.budget_summary(month, request.company_key)
            if request.maximum_microusd > summary["remaining_microusd"]:
                raise ValueError("monthly research budget would be exceeded")
            if request.maximum_microusd > summary["account"]["remaining_microusd"]:
                raise ValueError("per-account research budget would be exceeded")
            self.connection.execute(
                """INSERT INTO engine_budget_reservations(idempotency_key,company_key,month,
                provider,purpose,maximum_microusd,actual_microusd,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,NULL,'reserved',?,?)""",
                (
                    request.idempotency_key,
                    request.company_key,
                    month,
                    request.provider,
                    request.purpose,
                    request.maximum_microusd,
                    stamp,
                    stamp,
                ),
            )
        return {
            **dict(
                self.connection.execute(
                    "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                    (request.idempotency_key,),
                ).fetchone()
            ),
            "created": True,
        }

    def settle_budget(self, idempotency_key: str, actual_microusd: int) -> dict[str, Any]:
        if type(actual_microusd) is not int or actual_microusd < 0:
            raise ValueError("actual_microusd must be a nonnegative integer")
        with self._budget_transaction():
            reservation = self.connection.execute(
                "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if reservation is None:
                raise ValueError("budget reservation does not exist")
            if reservation["status"] == "cancelled":
                raise ValueError("cancelled budget reservations cannot be settled")
            if actual_microusd > reservation["maximum_microusd"]:
                raise ValueError("actual provider cost exceeds the requested cap")
            if reservation["status"] == "settled":
                if reservation["actual_microusd"] != actual_microusd:
                    raise ValueError("reservation was already settled with another actual cost")
                return dict(reservation)
            self.connection.execute(
                """UPDATE engine_budget_reservations SET actual_microusd=?,status='settled',
                updated_at=? WHERE idempotency_key=?""",
                (actual_microusd, _timestamp(), idempotency_key),
            )
        return dict(
            self.connection.execute(
                "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        )

    def cancel_budget(self, idempotency_key: str) -> dict[str, Any]:
        with self._budget_transaction():
            reservation = self.connection.execute(
                "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if reservation is None:
                raise ValueError("budget reservation does not exist")
            if reservation["status"] == "settled":
                raise ValueError("settled spend cannot be cancelled")
            if reservation["status"] == "cancelled":
                return dict(reservation)
            self.connection.execute(
                """UPDATE engine_budget_reservations SET actual_microusd=0,status='cancelled',
                updated_at=? WHERE idempotency_key=?""",
                (_timestamp(), idempotency_key),
            )
        return dict(
            self.connection.execute(
                "SELECT * FROM engine_budget_reservations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        )

    def learning_report(self, as_of: datetime | None = None) -> dict[str, Any]:
        from .learning import build_learning_report

        return build_learning_report(self.connection, as_of=as_of)
