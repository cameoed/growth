"""Append-only buyer facts on a caller-owned SQLite connection."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from .buyer import BuyerFact
from .models import normalize_company_key


class BuyerFactStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def _transaction(self):
        nested = self.connection.in_transaction
        savepoint = f"buyer_{uuid4().hex}"
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
        from .ranking import initialize_schema

        installed = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master")}
        if {"engine_buyer_facts", "idx_engine_buyer_facts_company"} <= installed:
            initialize_schema(self.connection)
            return
        with self._transaction():
            self.connection.execute("""CREATE TABLE IF NOT EXISTS engine_buyer_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                company_key TEXT NOT NULL, criterion TEXT NOT NULL,
                observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL)""")
            self.connection.execute("""CREATE INDEX IF NOT EXISTS idx_engine_buyer_facts_company
                ON engine_buyer_facts(company_key, observed_at, id)""")

            initialize_schema(self.connection)

    @staticmethod
    def _item(row) -> dict:
        return {**json.loads(row[2]), "id": row[0], "recorded_at": row[1]}

    def save(self, fact: BuyerFact) -> dict:
        fact = BuyerFact.model_validate(fact.model_dump())
        payload = json.dumps(
            fact.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self._transaction():
            row = self.connection.execute(
                "SELECT id,recorded_at,payload_json FROM engine_buyer_facts "
                "WHERE idempotency_key=?",
                (fact.idempotency_key,),
            ).fetchone()
            if row:
                if row[2] != payload:
                    raise ValueError(
                        "buyer fact idempotency key was already used for different content"
                    )
                return self._item(row)
            recorded_at = datetime.now(UTC).isoformat()
            cursor = self.connection.execute(
                """INSERT INTO engine_buyer_facts
                (idempotency_key,company_key,criterion,observed_at,recorded_at,payload_json)
                VALUES(?,?,?,?,?,?)""",
                (
                    fact.idempotency_key,
                    fact.company_key,
                    fact.criterion,
                    fact.observed_at.isoformat(),
                    recorded_at,
                    payload,
                ),
            )
            return {
                **fact.model_dump(mode="json"),
                "id": cursor.lastrowid,
                "recorded_at": recorded_at,
            }

    def list(self, company_key: str) -> list[dict]:
        key = normalize_company_key(company_key)
        return [
            self._item(row)
            for row in self.connection.execute(
                """SELECT id,recorded_at,payload_json FROM engine_buyer_facts
            WHERE company_key=? ORDER BY observed_at DESC,id DESC""",
                (key,),
            ).fetchall()
        ]
