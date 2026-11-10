"""Additive mission metadata and exact shared canonical inventory, no collection."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from ..release_mode import requires_user_mission
from ..sales_engine.persistence import SalesEngineRepository
from .spec import VERSION, MissionSetup, compile_spec


def initialize(connection):
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_mission_members'"
    ).fetchone():
        ensure_policy(connection)
        initialize_refresh_index(connection)
        initialize_contact_revisions(connection)
        from .compiler import initialize_schema

        initialize_schema(connection)
        initialize_name_history(connection)
        initialize_review_cache(connection)
        from .read_indexes import initialize as initialize_reads

        initialize_reads(connection)
        from .compiler import reconcile_finished

        reconcile_finished(connection)
        return
    repo = SalesEngineRepository(connection)
    with repo._transaction():
        connection.execute("""CREATE TABLE engine_missions(
            id TEXT PRIMARY KEY,name TEXT NOT NULL,brief TEXT NOT NULL,spec_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE engine_mission_members(
            candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
            company_key TEXT NOT NULL,snapshot_id INTEGER,company_name TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1)""")
        connection.execute("""CREATE INDEX idx_mission_members_company
            ON engine_mission_members(company_key,snapshot_id DESC,candidate_id)""")
        connection.execute("""CREATE TABLE engine_mission_scores(
            mission_id TEXT NOT NULL REFERENCES engine_missions(id),company_key TEXT NOT NULL,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,input_version TEXT NOT NULL,policy_version TEXT NOT NULL,
            score INTEGER,as_of TEXT NOT NULL,valid_until TEXT NOT NULL,audit_json TEXT NOT NULL,
            PRIMARY KEY(mission_id,company_key))""")
        connection.execute("""CREATE INDEX idx_mission_scores_order
            ON engine_mission_scores(mission_id,score DESC,company_key)""")
        connection.execute("""CREATE TABLE engine_mission_reviews(
            cache_key TEXT PRIMARY KEY,mission_id TEXT NOT NULL REFERENCES engine_missions(id),
            company_key TEXT NOT NULL,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,input_version TEXT NOT NULL,model TEXT NOT NULL,
            payload_json TEXT NOT NULL,recorded_at TEXT NOT NULL,valid_until TEXT NOT NULL)""")
        connection.execute("""CREATE INDEX idx_mission_reviews_company
            ON engine_mission_reviews(company_key,mission_id)""")
        connection.execute("""CREATE TABLE engine_mission_inventory(
            company_key TEXT PRIMARY KEY,candidate_id INTEGER,snapshot_id INTEGER,
            company_name TEXT NOT NULL,input_version TEXT NOT NULL)""")
        connection.execute("""CREATE INDEX idx_mission_inventory_name
            ON engine_mission_inventory(company_name,company_key)""")
        select = """SELECT c.id,
            COALESCE(NULLIF(ti.canonical_domain,''),c.hostname),s.id,
            COALESCE(NULLIF(ti.company_name,''),NULLIF(c.label_hint,''),c.hostname),1
            FROM candidates c LEFT JOIN snapshots s ON s.id=(
                SELECT MAX(id) FROM snapshots WHERE candidate_id=c.id)
            LEFT JOIN snapshot_target_intelligence ti ON ti.snapshot_id=s.id"""
        # ti does not expose a stable display name on all legacy installations.
        select = select.replace("NULLIF(ti.company_name,''),", "")
        connection.execute("INSERT INTO engine_mission_members " + select)
        for table, key in [
            ("candidates", "id"),
            ("snapshots", "candidate_id"),
            ("snapshot_target_intelligence", "candidate_id"),
        ]:
            for action in ["INSERT", "UPDATE", "DELETE"]:
                refs = (
                    ["OLD", "NEW"]
                    if action == "UPDATE"
                    else ["OLD" if action == "DELETE" else "NEW"]
                )
                body = ""
                for ref in refs:
                    body += (
                        """INSERT INTO engine_mission_members """
                        + select
                        + f" WHERE c.id={ref}.{key} ON CONFLICT(candidate_id) DO UPDATE SET "
                        "company_key=excluded.company_key,snapshot_id=excluded.snapshot_id,"
                        "company_name=excluded.company_name,revision=revision+1;"
                    )
                connection.execute(
                    f"CREATE TRIGGER mission_member_{table}_{action} AFTER "
                    f"{action} ON {table} BEGIN {body} END"
                )

        def inventory_update(key):
            return f"""INSERT INTO engine_mission_inventory
                SELECT {key},candidate_id,snapshot_id,company_name,'1'
                FROM engine_mission_members WHERE company_key={key}
                AND candidate_id=(SELECT candidate_id FROM engine_mission_members
                    WHERE company_key={key} ORDER BY snapshot_id DESC,candidate_id LIMIT 1)
                ON CONFLICT(company_key) DO UPDATE SET candidate_id=excluded.candidate_id,
                    snapshot_id=excluded.snapshot_id,company_name=excluded.company_name,
                    input_version=CAST(CAST(input_version AS INTEGER)+1 AS TEXT);
                INSERT INTO engine_mission_inventory
                SELECT {key},NULL,NULL,company,'1' FROM sales_accounts
                WHERE website_host={key} AND id=(SELECT MAX(id) FROM sales_accounts
                    WHERE website_host={key}) AND NOT EXISTS(SELECT 1 FROM engine_mission_members
                    WHERE company_key={key})
                ON CONFLICT(company_key) DO UPDATE SET candidate_id=NULL,snapshot_id=NULL,
                    company_name=excluded.company_name,
                    input_version=CAST(CAST(input_version AS INTEGER)+1 AS TEXT);
                DELETE FROM engine_mission_inventory WHERE company_key={key}
                    AND NOT EXISTS(SELECT 1 FROM engine_mission_members WHERE company_key={key})
                    AND NOT EXISTS(SELECT 1 FROM sales_accounts WHERE website_host={key});
                DELETE FROM engine_mission_scores WHERE company_key={key} AND
                    NOT EXISTS(SELECT 1 FROM engine_mission_inventory WHERE company_key={key});
                DELETE FROM engine_mission_reviews WHERE company_key={key} AND
                    NOT EXISTS(SELECT 1 FROM engine_mission_inventory WHERE company_key={key});"""

        for table, field in [
            ("engine_mission_members", "company_key"),
            ("sales_accounts", "website_host"),
        ]:
            for action in ["INSERT", "UPDATE", "DELETE"]:
                refs = (
                    ["OLD", "NEW"]
                    if action == "UPDATE"
                    else ["OLD" if action == "DELETE" else "NEW"]
                )
                body = "".join(inventory_update(f"{ref}.{field}") for ref in refs)
                connection.execute(
                    f"CREATE TRIGGER mission_inventory_{table}_{action} "
                    f"AFTER {action} ON {table} BEGIN {body} END"
                )
        connection.execute("""INSERT INTO engine_mission_inventory
            SELECT a.company_key,a.candidate_id,a.snapshot_id,a.company_name,'1'
            FROM engine_mission_members a WHERE candidate_id=(
                SELECT candidate_id FROM engine_mission_members b WHERE b.company_key=a.company_key
                ORDER BY snapshot_id DESC,candidate_id LIMIT 1)""")
        connection.execute("""INSERT OR IGNORE INTO engine_mission_inventory
            SELECT website_host,NULL,NULL,company,'1' FROM sales_accounts
            WHERE website_host IS NOT NULL ORDER BY id DESC""")
        for action in ["INSERT", "UPDATE"]:
            connection.execute(f"""CREATE TRIGGER mission_revision_{action} AFTER {action}
                ON engine_ranking_revisions BEGIN
                UPDATE engine_mission_members SET revision=revision+1
                WHERE candidate_id=NEW.candidate_id; END""")
        if not requires_user_mission():
            connection.execute(
                "INSERT INTO engine_missions VALUES(?,?,?,?,1,0,?)",
                (
                    "beings",
                    "Beings",
                    "$5,000/month creative subscription",
                    json.dumps({"policy": "beings_unchanged"}),
                    datetime.now(UTC).isoformat(),
                ),
            )

    ensure_policy(connection)
    initialize_refresh_index(connection)
    initialize_contact_revisions(connection)
    from .compiler import initialize_schema

    initialize_schema(connection)
    initialize_name_history(connection)
    initialize_review_cache(connection)
    from .read_indexes import initialize as initialize_reads

    initialize_reads(connection)


def initialize_contact_revisions(connection):
    from ..treg_people import SCANNER_ID

    # A successful empty people scan can withdraw a prior accepted contact packet.
    # Beings' write graph intentionally treats these execution receipts differently.
    for action, refs in (
        ("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",)),
    ):
        keys = " UNION ".join(
            f"SELECT {ref}.company_key WHERE {ref}.scanner_id='{SCANNER_ID}' "
            f"AND {ref}.status IN ('success','success_empty')" for ref in refs
        )
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS mission_people_{action.lower()}
            AFTER {action} ON scanner_executions BEGIN
            UPDATE engine_mission_inventory
            SET input_version=CAST(CAST(input_version AS INTEGER)+1 AS TEXT)
            WHERE company_key IN ({keys}); END""")


def initialize_review_cache(connection):
    """Separate reusable semantic content from mission-owned application records."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='engine_mission_review_cache'"
    ).fetchone()
    with SalesEngineRepository(connection)._transaction():
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_mission_review_cache(
            cache_key TEXT PRIMARY KEY,model TEXT NOT NULL,payload_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL)""")
        if not exists:
            connection.execute("""INSERT OR IGNORE INTO engine_mission_review_cache
                SELECT cache_key,model,payload_json,recorded_at FROM engine_mission_reviews""")


def initialize_refresh_index(connection):
    """Cover exact rebuild metadata without scanning full audit bodies for selection."""
    from .bridge import initialize_indexes

    with SalesEngineRepository(connection)._transaction():
        initialize_indexes(connection)
        connection.execute("""CREATE INDEX IF NOT EXISTS idx_mission_scores_refresh
            ON engine_mission_scores(mission_id,company_key,revision,input_version,
                policy_version,as_of,valid_until,score)""")


def initialize_name_history(connection):
    with SalesEngineRepository(connection)._transaction():
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_mission_name_changes(
            id INTEGER PRIMARY KEY,mission_id TEXT NOT NULL REFERENCES engine_missions(id),
            previous_name TEXT NOT NULL,name TEXT NOT NULL,changed_at TEXT NOT NULL)""")


def update_mission(connection, mission_id, *, name=None, archived=None):
    """Cosmetic metadata only: never revise the rubric, scores, review or budget identities."""
    if name is not None:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Mission title must contain 1 to 80 characters")
    with SalesEngineRepository(connection)._transaction():
        current = get_mission(connection, mission_id, archived=True)
        if name is not None and name != current["name"]:
            connection.execute(
                "INSERT INTO engine_mission_name_changes(mission_id,previous_name,name,changed_at) "
                "VALUES(?,?,?,?)",
                (mission_id, current["name"], name, datetime.now(UTC).isoformat()),
            )
            connection.execute("UPDATE engine_missions SET name=? WHERE id=?", (name, mission_id))
        if archived is not None:
            connection.execute(
                "UPDATE engine_missions SET archived=? WHERE id=?", (int(archived), mission_id)
            )
    return list_missions(connection)


def ensure_policy(connection):
    """Upgrade derived rubrics atomically, retaining each old specification in the active DB."""
    rows = connection.execute("SELECT * FROM engine_missions WHERE id<>'beings'").fetchall()
    missing_history = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='engine_mission_versions'"
        ).fetchone()
        is None
    )
    outdated = [r for r in rows if json.loads(r["spec_json"]).get("version") != VERSION]
    if not missing_history and not outdated:
        return
    with SalesEngineRepository(connection)._transaction():
        connection.execute("""CREATE TABLE IF NOT EXISTS engine_mission_versions(
            mission_id TEXT NOT NULL REFERENCES engine_missions(id),revision INTEGER NOT NULL,
            name TEXT NOT NULL,brief TEXT NOT NULL,spec_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(mission_id,revision))""")
        for row in rows:
            connection.execute(
                "INSERT OR IGNORE INTO engine_mission_versions VALUES(?,?,?,?,?,?)",
                (
                    row["id"],
                    row["revision"],
                    row["name"],
                    row["brief"],
                    row["spec_json"],
                    datetime.now(UTC).isoformat(),
                ),
            )
        for row in outdated:
            try:
                previous = json.loads(row["spec_json"])
                if previous.get("compiler") == "ai-rubric-v1":
                    from .rubric import Rubric, as_spec

                    compiled = as_spec(
                        Rubric.model_validate(previous["rubric"]),
                        row["brief"],
                        version=VERSION,
                        model=previous["compiler_model"],
                        cache_key=previous["compiler_cache_key"],
                        setup=MissionSetup.model_validate(previous["setup"])
                        if "setup" in previous else None,
                    )
                    compiled["compiled_on"] = previous["compiled_on"]
                    if "discovery_terms_v1" in previous:
                        compiled["discovery_terms_v1"] = previous["discovery_terms_v1"]
                elif previous.get("compiler_pending"):
                    compiled = {**previous, "version": VERSION}
                else:
                    compiled = compile_spec(row["brief"])
            except ValueError:
                compiled = {
                    "version": VERSION,
                    "topics": [],
                    "geography": [],
                    "formats": [],
                    "relation": "matching_company",
                    "contact_terms": [],
                    "brief": row["brief"],
                    "ai_adjustment_limit": 20,
                    "compiler_error": "No supported core category",
                }
            revision = row["revision"] + 1
            encoded = json.dumps(compiled)
            connection.execute(
                "UPDATE engine_missions SET spec_json=?,revision=? WHERE id=?",
                (encoded, revision, row["id"]),
            )
            connection.execute(
                "INSERT INTO engine_mission_versions VALUES(?,?,?,?,?,?)",
                (
                    row["id"],
                    revision,
                    row["name"],
                    row["brief"],
                    encoded,
                    datetime.now(UTC).isoformat(),
                ),
            )


MISSION_BY_ID_SQL = "SELECT * FROM engine_missions WHERE id=?"


def get_mission(connection, mission_id, *, archived=False):
    if mission_id == "beings" and requires_user_mission():
        raise LookupError("Mission not found")
    row = connection.execute(MISSION_BY_ID_SQL, (mission_id,)).fetchone()
    if row is None or (row["archived"] and not archived):
        raise LookupError("Mission not found")
    return {**dict(row), "spec": json.loads(row["spec_json"])}


def list_missions(connection):
    from .compiler import DEFAULT_MODEL, MODELS, REASONING_EFFORTS, status

    public = requires_user_mission()
    items = [
        dict(row)
        for row in connection.execute(
            "SELECT id,name,brief,revision,archived FROM engine_missions ORDER BY created_at,id"
        )
        if not public or row["id"] != "beings"
    ]
    for item in items:
        item["compilation"] = status(connection, get_mission(connection, item["id"], archived=True))
    return {
        "items": items,
        "default_mission_id": (
            next((row["id"] for row in items if not row["archived"]), None)
            if public else "beings"
        ),
        "provider_requests": 0,
        "compiler": {
            "default_model": DEFAULT_MODEL,
            "models": [
                {"id": model, "maximum_microusd": rates[2],
                 "reasoning_effort": REASONING_EFFORTS[model]}
                for model, rates in MODELS.items()
            ],
        },
    }


def create_mission(connection, name, brief, *, pending_ai=False, setup=None, compiler_model=None):
    if setup is not None and not pending_ai:
        raise ValueError("Guided mission setup requires AI compilation")
    spec = (
        {
            "version": VERSION,
            "brief": brief,
            "topics": [],
            "geography": [],
            "formats": [],
            "relation": "matching_company",
            "contact_terms": [],
            "ai_adjustment_limit": 20,
            "compiler_pending": True,
            "business_role": {"id": "business_role", "terms": []},
        }
        if pending_ai
        else compile_spec(brief)
    )
    if setup is not None:
        spec["setup"] = setup.model_dump()
    if pending_ai and compiler_model is not None:
        spec["compiler_model"] = compiler_model
    mission_id = uuid4().hex
    with SalesEngineRepository(connection)._transaction():
        connection.execute(
            "INSERT INTO engine_missions VALUES(?,?,?,?,1,0,?)",
            (mission_id, name, brief, json.dumps(spec), datetime.now(UTC).isoformat()),
        )
        connection.execute(
            "INSERT INTO engine_mission_versions VALUES(?,?,?,?,?,?)",
            (mission_id, 1, name, brief, json.dumps(spec), datetime.now(UTC).isoformat()),
        )
    return get_mission(connection, mission_id)


def archive_mission(connection, mission_id, archived):
    return update_mission(connection, mission_id, archived=archived)


def run_mission(connection, mission_id):
    """Queue a complete provider-free rebuild; keep source data and paid interpretations."""
    with SalesEngineRepository(connection)._transaction():
        get_mission(connection, mission_id)
        if mission_id == "beings":
            from ..sales_engine.ranking import policy_version

            connection.execute("UPDATE engine_company_ranking SET dirty=1")
            connection.execute(
                "INSERT INTO engine_ranking_backfill "
                "(policy_version,last_candidate_id,complete,updated_at,live_phase) "
                "VALUES(?,0,0,?,1) ON CONFLICT(policy_version) DO UPDATE SET "
                "last_candidate_id=0,complete=0,updated_at=excluded.updated_at,live_phase=1",
                (policy_version(), datetime.now(UTC).isoformat()),
            )
        else:
            connection.execute(
                "UPDATE engine_mission_scores SET valid_until=? WHERE mission_id=?",
                ("1970-01-01T00:00:00+00:00", mission_id),
            )
    return {"mission_id": mission_id, "status": "queued", "provider_requests": 0}


def delete_mission(connection, mission_id):
    """Delete archived mission-owned state, never shared evidence or financial receipts."""
    if mission_id == "beings":
        raise ValueError("The default Beings mission cannot be permanently deleted")
    with SalesEngineRepository(connection)._transaction():
        current = connection.execute(
            "SELECT archived FROM engine_missions WHERE id=?", (mission_id,)
        ).fetchone()
        if current:
            if not current["archived"]:
                raise ValueError("Remove the mission before permanently deleting it")
            if connection.execute(
                "SELECT 1 FROM engine_mission_compiler_requests r "
                "LEFT JOIN engine_mission_compilations c ON c.cache_key=r.cache_key "
                "WHERE r.mission_id=? AND r.status IN ('running','uncertain') "
                "AND (c.cache_key IS NULL OR c.status IN ('running','uncertain'))",
                (mission_id,),
            ).fetchone():
                raise ValueError("Mission compilation is running or needs cost reconciliation")
            for table in (
                "engine_mission_scores",
                "engine_mission_reviews",
                "engine_mission_versions",
                "engine_mission_name_changes",
                "engine_mission_compiler_requests",
                "engine_mission_compiler_bindings",
            ):
                connection.execute(f"DELETE FROM {table} WHERE mission_id=?", (mission_id,))
            connection.execute("DELETE FROM engine_missions WHERE id=?", (mission_id,))
    return list_missions(connection)
