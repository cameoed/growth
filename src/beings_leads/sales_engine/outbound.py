"""Operator-saved people and manual outbound stages; never scoring or outreach."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from datetime import UTC, datetime

from ..domain_policy import require_allowed

CONTACT_METHODS = {"": "Not set", "email": "Email", "linkedin": "LinkedIn",
                   "phone": "Phone", "other": "Other"}

STATUSES = {
    "saved": "Saved",
    "contacted": "Contacted",
    "replied": "Replied",
    "interested": "Interested",
    "meeting_booked": "Meeting booked",
    "closed_won": "Closed Won",
    "not_interested": "Not interested",
    "do_not_contact": "Do not contact",
}


def identity_text(value):
    return unicodedata.normalize("NFKC", " ".join(str(value or "").split())).casefold()


def person_key(company, person):
    profile = str(person.get("profile_url") or "").lower().rstrip("/")
    identity = (
        "profile:" + profile if profile else
        "email:" + identity_text(person["email"])
        if person.get("source_kind") == "operator_supplied" and person.get("email") else
        "phone:" + person["phone"]
        if person.get("source_kind") == "operator_supplied" and person.get("phone")
        and person["name"] == person["phone"] else
        "name:" + identity_text(person["name"])
    )
    return hashlib.sha256((company.lower() + "\n" + identity).encode()).hexdigest()


def initialize(connection):
    from .contact_lookup import initialize as initialize_contact_lookups

    initialize_contact_lookups(connection)
    allowed = ",".join("'" + key + "'" for key in STATUSES)
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbound_people'"
    ).fetchone()
    if existing and "'closed_won'" not in existing[0]:
        _upgrade_status_constraint(connection, existing[0], allowed)
    connection.execute(f"""CREATE TABLE IF NOT EXISTS outbound_people (
      id INTEGER PRIMARY KEY, company_key TEXT NOT NULL,
      candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
      person_key TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
      title TEXT NOT NULL, email TEXT, profile_url TEXT, company_name TEXT NOT NULL,
      contact_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'saved'
        CHECK(status IN ({allowed})), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(company_key,person_key))""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(outbound_people)")}
    if "note" not in columns:
        connection.execute("ALTER TABLE outbound_people ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "contact_method" not in columns:
        connection.execute(
            "ALTER TABLE outbound_people ADD COLUMN contact_method TEXT NOT NULL DEFAULT '' "
            "CHECK(contact_method IN ('','email','linkedin','phone','other'))"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_status "
        "ON outbound_people(status,updated_at DESC,id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_identity ON outbound_people(company_key,name_key)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS outbound_status_history (
      id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL
        REFERENCES outbound_people(id) ON DELETE CASCADE,
      from_status TEXT, to_status TEXT NOT NULL, changed_at TEXT NOT NULL)""")

    connection.execute("""CREATE TABLE IF NOT EXISTS outbound_contact_touchpoints (
      id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL
        REFERENCES outbound_people(id) ON DELETE CASCADE,
      contact_method TEXT NOT NULL CHECK(contact_method IN ('email','linkedin','phone','other')),
      contacted_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_outbound_touchpoints "
                       "ON outbound_contact_touchpoints(person_id,id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_outbound_contacted_history "
                       "ON outbound_status_history(person_id,to_status,changed_at)")


def _upgrade_status_constraint(connection, schema, allowed):
    """Rebuild the CHECK atomically, retaining all people and transition IDs."""
    revised, count = re.subn(
        r"CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)",
        f"CHECK(status IN ({allowed}))",
        schema,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Unrecognized saved-person status constraint")
    revised = revised.replace("outbound_people", "outbound_people_updated", 1)
    connection.execute("SAVEPOINT outbound_status_upgrade")
    try:
        # Stage history inside the active DB: dropping its parent cascades under FK ON.
        connection.execute(
            "CREATE TABLE outbound_history_migration AS SELECT * FROM outbound_status_history"
        )
        has_touchpoints = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='outbound_contact_touchpoints'").fetchone()
        if has_touchpoints:
            connection.execute("CREATE TABLE outbound_touchpoints_migration AS "
                               "SELECT * FROM outbound_contact_touchpoints")
        connection.execute(revised)
        connection.execute("INSERT INTO outbound_people_updated SELECT * FROM outbound_people")
        connection.execute("DROP TABLE outbound_people")
        connection.execute("ALTER TABLE outbound_people_updated RENAME TO outbound_people")
        connection.execute("DELETE FROM outbound_status_history")
        connection.execute(
            "INSERT INTO outbound_status_history SELECT * FROM outbound_history_migration"
        )
        connection.execute("DROP TABLE outbound_history_migration")
        if has_touchpoints:
            connection.execute("DELETE FROM outbound_contact_touchpoints")
            connection.execute("INSERT INTO outbound_contact_touchpoints "
                               "SELECT * FROM outbound_touchpoints_migration")
            connection.execute("DROP TABLE outbound_touchpoints_migration")
        connection.execute(
            "CREATE INDEX idx_outbound_status ON outbound_people(status,updated_at DESC,id DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_outbound_identity ON outbound_people(company_key,name_key)"
        )
        for table in ("outbound_people", "outbound_status_history"):
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchone():
                raise ValueError("Saved-person migration failed its foreign-key check")
        connection.execute("RELEASE outbound_status_upgrade")
    except BaseException:
        connection.execute("ROLLBACK TO outbound_status_upgrade")
        connection.execute("RELEASE outbound_status_upgrade")
        raise


def _stamp():
    return datetime.now(UTC).isoformat()


def _effective_phone(contact):
    updates = contact.get("operator_updates", {})
    if "phone" in updates:
        return updates["phone"]
    return ((contact.get("operator_contact") or {}).get("phone") or contact.get("phone")
            or (contact.get("public_work_phone") or {}).get("value")
            or contact.get("ai_contact", {}).get("phone", {}).get("value"))


def _item(row, connection):
    result = dict(row)
    result.pop("name_key", None)
    contact = json.loads(result.pop("contact_json", "{}"))
    result["ai_contact"] = contact.get("ai_contact", {})
    result["phone"] = _effective_phone(contact)
    result["public_work_phone"] = contact.get("public_work_phone")
    result["phone_lookup_blocked"] = (
        "phone" in contact.get("operator_updates", {}) and not result["phone"]
    )
    result["operator_supplied"] = (
        contact.get("source_kind") == "operator_supplied" or bool(contact.get("operator_created"))
    )
    result["has_note"] = bool(result.pop("note", ""))
    result["touchpoints"] = [dict(r) for r in connection.execute(
        "SELECT id,contact_method,contacted_at FROM outbound_contact_touchpoints "
        "WHERE person_id=? ORDER BY id", (result["id"],))]
    result["contacted_at"] = connection.execute(
        "SELECT MIN(changed_at) FROM outbound_status_history "
        "WHERE person_id=? AND to_status='contacted'", (result["id"],)).fetchone()[0]
    return result


def save(connection, packet, key, *, _operator=False):
    """Packet must come from the app's current contact projection, never the client."""
    company = packet["company_key"]
    person = next(
        (
            p
            for p in packet.get("contact_recommendations", {}).get("people", [])
            if person_key(company, p) == key
        ),
        None,
    )
    if person is None or not packet.get("candidate_id"):
        raise LookupError("This person is no longer in the company's current contacts")
    connection.execute("BEGIN IMMEDIATE")
    try:
        require_allowed(company)
        candidate = connection.execute(
            "SELECT hostname FROM candidates WHERE id=?", (packet["candidate_id"],)
        ).fetchone()
        if candidate is None:
            raise LookupError("Company no longer exists")
        require_allowed(candidate[0])
        if _operator:
            from .summaries import _identity
            if _identity(connection, packet["candidate_id"])["company_key"] != company:
                raise ValueError("Company identity changed; reopen the company and retry")
        stamp = _stamp()
        row = connection.execute(
            "SELECT * FROM outbound_people WHERE company_key=? AND person_key=?", (company, key)
        ).fetchone()
        if row is None:
            # A newly discovered profile can enrich a previously saved name-only person.
            matches = connection.execute(
                "SELECT * FROM outbound_people WHERE company_key=? AND name_key=?",
                (company, identity_text(person["name"])),
            ).fetchall()
            if _operator and person.get("email"):
                matches = connection.execute(
                    "SELECT * FROM outbound_people WHERE company_key=? "
                    "AND (name_key=? OR email=? COLLATE NOCASE)",
                    (company, identity_text(person["name"]), person["email"]),
                ).fetchall()
            if not _operator and person.get("email"):
                manual_matches = connection.execute(
                    "SELECT * FROM outbound_people WHERE company_key=? "
                    "AND email=? COLLATE NOCASE AND "
                    "(json_extract(contact_json,'$.source_kind')='operator_supplied' "
                    "OR json_extract(contact_json,'$.operator_created')=1)",
                    (company, person["email"]),
                ).fetchall()
                matches = list({r["id"]: r for r in [*matches, *manual_matches]}.values())
            eligible = [r for r in matches if (
                not r["profile_url"] or not person.get("profile_url")
                or (_operator and r["profile_url"].lower().rstrip("/")
                    == person["profile_url"].lower().rstrip("/"))
            ) and (not _operator or not r["email"] or not person.get("email")
                   or r["email"].casefold() == person["email"].casefold())]
            if len(eligible) == 1 and (_operator or len(matches) == 1):
                row = eligible[0]
        if row is None:
            cursor = connection.execute(
                """INSERT INTO outbound_people
              (company_key,candidate_id,person_key,name,name_key,title,email,profile_url,
               company_name,contact_json,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    company,
                    packet["candidate_id"],
                    key,
                    person["name"],
                    identity_text(person["name"]),
                    person.get("title") or "",
                    person.get("email"),
                    person.get("profile_url"),
                    packet.get("company_name") or company,
                    json.dumps(person),
                    stamp,
                    stamp,
                ),
            )
            pid = cursor.lastrowid
            connection.execute(
                "INSERT INTO outbound_status_history(person_id,to_status,changed_at) VALUES(?,?,?)",
                (pid, "saved", stamp),
            )
        else:
            pid = row["id"]
            # Operator entries fill gaps without replacing saved source evidence.
            if _operator:
                original = json.loads(row["contact_json"])
                operator = original.get("operator_contact") or (
                    dict(original) if original.get("source_kind") == "operator_supplied" else {}
                )
                operator.update({field: value for field, value in person.items()
                                 if value not in (None, "") and not (
                                     field == "name" and value == person.get("email")
                                     and operator.get("name"))})
                original["operator_contact"] = operator
                contact_json = json.dumps(original)
                route_name = row["name"].casefold() in {
                    (row["email"] or "").casefold(), _effective_phone(original) or "",
                }
                person = {
                    **person,
                    "name": person["name"] if route_name else row["name"],
                    "title": row["title"] or person.get("title"),
                    "email": row["email"] or person.get("email"),
                    "profile_url": row["profile_url"] or person.get("profile_url"),
                }
            else:
                previous = json.loads(row["contact_json"])
                snapshot = dict(person)
                operator_created = (previous.get("source_kind") == "operator_supplied"
                                    or previous.get("operator_created"))
                if operator_created or previous.get("operator_contact"):
                    snapshot["operator_contact"] = previous.get("operator_contact", previous)
                    snapshot["operator_created"] = bool(operator_created)
                    person = {**person, "title": person.get("title") or row["title"],
                              "email": person.get("email") or row["email"]}
                contact_json = json.dumps(snapshot)
            previous_contact = json.loads(row["contact_json"])
            if previous_contact.get("ai_contact"):
                snapshot = json.loads(contact_json)
                snapshot["ai_contact"] = previous_contact["ai_contact"]
                contact_json = json.dumps(snapshot)
                person = {**person, "email": person.get("email") or row["email"]}
            if previous_contact.get("public_work_phone"):
                snapshot = json.loads(contact_json)
                snapshot["public_work_phone"] = previous_contact["public_work_phone"]
                contact_json = json.dumps(snapshot)
            if "operator_updates" in previous_contact:
                updated_contact = json.loads(contact_json)
                for field in ("operator_updates", "operator_edit_history"):
                    if field in previous_contact:
                        updated_contact[field] = previous_contact[field]
                contact_json = json.dumps(updated_contact)
                person = {**person, **previous_contact["operator_updates"]}
            # Saving again refreshes evidence, retaining status, notes and original creation.
            connection.execute(
                """UPDATE outbound_people SET candidate_id=?,person_key=?,
              name=?,name_key=?,title=?,email=?,profile_url=?,company_name=?,
              contact_json=? WHERE id=?""",
                (
                    packet["candidate_id"],
                    row["person_key"] if _operator and row["profile_url"] else
                    key if person.get("profile_url") else row["person_key"],
                    person["name"],
                    identity_text(person["name"]),
                    person.get("title") or "",
                    person.get("email"),
                    person.get("profile_url") if "operator_updates" in previous_contact else
                    person.get("profile_url") or row["profile_url"],
                    packet.get("company_name") or company,
                    contact_json,
                    pid,
                ),
            )
        result = _item(
            connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone(),
            connection
        )
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def fill_contact_gaps(connection, packet, key, contacts, *, observed_at, model=None,
                      expected_identity=None):
    """Keep sourced contact findings separate from operator fields and original evidence."""
    from .contact_lookup import resolve_person
    from .summaries import _identity

    company = packet["company_key"]
    # Recheck identity after the network wait, before saving anything.
    if _identity(connection, packet["candidate_id"])["company_key"] != company:
        raise ValueError("Company identity changed; reopen this company")
    person = resolve_person(connection, packet, key)
    if expected_identity and (person["name"], person.get("profile_url")) != expected_identity:
        raise ValueError("Contact identity changed; reopen this company")
    saved = connection.execute(
        "SELECT id FROM outbound_people WHERE company_key=? AND person_key=?", (company, key)
    ).fetchone()
    if saved is None:
        saved = save(connection, packet, key)
        pid = saved["id"]
    else:
        pid = saved["id"]
    connection.execute("BEGIN IMMEDIATE")
    try:
        require_allowed(company)
        if _identity(connection, packet["candidate_id"])["company_key"] != company:
            raise ValueError("Company identity changed; reopen this company")
        row = connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone()
        if expected_identity and (row["name"], row["profile_url"]) != expected_identity:
            raise ValueError("Contact identity changed; reopen this company")
        original = json.loads(row["contact_json"])
        evidence = original.setdefault("ai_contact", {})
        updates = original.get("operator_updates", {})
        email = row["email"]
        changed = False
        for field, value in contacts.items():
            if (not value or field in updates
                    or (field == "email" and (email or person.get("email")))
                    or (field == "phone" and (_effective_phone(original) or person.get("phone")))):
                continue
            if field == "phone" and model is None:
                original["public_work_phone"] = {
                    **value, "observed_at": observed_at,
                    "source_kind": value.get("source_kind", "public_first_party"),
                }
            else:
                evidence[field] = {**value, "observed_at": observed_at, "model": model,
                                   "source_kind": "ai_public_web_search"}
            if field == "email":
                conflict = connection.execute(
                    "SELECT 1 FROM outbound_people WHERE company_key=? AND id<>? "
                    "AND email=? COLLATE NOCASE", (company, pid, value["value"]),
                ).fetchone()
                if conflict:
                    evidence.pop(field)
                    continue
                email = value["value"]
            changed = True
        if changed:
            connection.execute(
                "UPDATE outbound_people SET email=?,contact_json=?,updated_at=? WHERE id=?",
                (email, json.dumps(original), _stamp(), pid),
            )
        result = _item(connection.execute(
            "SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone(), connection)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def set_status(connection, pid, status):
    if status not in STATUSES:
        raise ValueError("Unknown prospect status")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise LookupError("Saved person not found")
        require_allowed(row["company_key"])
        candidate = connection.execute(
            "SELECT hostname FROM candidates WHERE id=?", (row["candidate_id"],)
        ).fetchone()
        if candidate is None:
            raise LookupError("Company no longer exists")
        require_allowed(candidate[0])
        if row["status"] != status:
            stamp = _stamp()
            connection.execute(
                "UPDATE outbound_people SET status=?,updated_at=? WHERE id=?", (status, stamp, pid)
            )
            connection.execute(
                "INSERT INTO outbound_status_history(person_id,from_status,to_status,changed_at) "
                "VALUES(?,?,?,?)",
                (pid, row["status"], status, stamp),
            )
        result = _item(
            connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone(),
            connection
        )
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def list_people(connection, *, status="", search="", offset=0, limit=50, company_key=None):
    if status and status not in STATUSES:
        raise ValueError("Unknown prospect status")
    connection.execute("BEGIN")
    try:
        conditions, args = [], []
        if company_key:
            conditions.append("company_key=?")
            args.append(company_key)
        if search.strip():
            # Literal search, including percent/underscore characters.
            term = (
                "%"
                + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            conditions.append(
                "(name LIKE ? ESCAPE '\\' OR company_name LIKE ? ESCAPE '\\' "
                "OR company_key LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\')"
            )
            args.extend([term] * 4)
        base = " AND ".join(conditions) or "1=1"
        counts = {s: 0 for s in STATUSES}
        for row in connection.execute(
            f"SELECT status,COUNT(*) FROM outbound_people WHERE {base} GROUP BY status", args
        ):
            counts[row[0]] = row[1]
        if status:
            conditions.append("status=?")
            args.append(status)
        where = " AND ".join(conditions) or "1=1"
        total = connection.execute(
            f"SELECT COUNT(*) FROM outbound_people WHERE {where}", args
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM outbound_people WHERE {where} "
            "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {
            "items": [_item(r, connection) for r in rows],
            "total": total,
            "counts": counts,
            "statuses": STATUSES,
            "contact_methods": CONTACT_METHODS,
            "offset": offset,
            "limit": limit,
        }
    finally:
        connection.rollback()


def _csv_cell(value):
    text = "" if value is None else str(value)
    return f"'{text}" if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_people_csv(connection):
    """Export the full saved-person list from a single read snapshot."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Name", "Role", "Company", "Domain", "Email", "Phone", "Profile URL",
        "Status", "Contact Method", "First Contacted At", "Touchpoints", "Updated At", "Notes",
    ])
    connection.execute("BEGIN")
    try:
        rows = connection.execute("SELECT * FROM outbound_people ORDER BY updated_at DESC,id DESC")
        for row in rows:
            person = _item(row, connection)
            touchpoints = "\n".join(
                f"{CONTACT_METHODS[touch['contact_method']]} · {touch['contacted_at']}"
                for touch in person["touchpoints"]
            )
            writer.writerow(map(_csv_cell, [
                person["name"], person["title"], person["company_name"], person["company_key"],
                person["email"], person["phone"], person["profile_url"], STATUSES[person["status"]],
                CONTACT_METHODS[person["contact_method"]],
                person["contacted_at"], touchpoints, person["updated_at"], row["note"],
            ]))
    finally:
        connection.rollback()
    return output.getvalue()


def person_note(connection, pid, note=None):
    """Read or explicitly replace operator text without changing outreach stage."""
    if note is not None and (not isinstance(note, str) or len(note) > 100_000):
        raise ValueError("Note must be text of at most 100,000 characters")
    connection.execute("BEGIN IMMEDIATE" if note is not None else "BEGIN")
    try:
        row = connection.execute(
            "SELECT p.note,p.company_key,c.hostname FROM outbound_people p "
            "JOIN candidates c ON c.id=p.candidate_id WHERE p.id=?", (pid,)
        ).fetchone()
        if row is None:
            raise LookupError("Saved person not found")
        require_allowed(row["company_key"])
        require_allowed(row["hostname"])
        if note is not None:
            connection.execute(
                "UPDATE outbound_people SET note=?,updated_at=? WHERE id=?",
                (note, _stamp(), pid),
            )
        result = {"id": pid, "note": row["note"] if note is None else note}
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def save_manual(connection, candidate_id, *, name="", title="", email="", profile_url="",
                phone=None):
    """Explicit operator contact, attached to server-resolved company identity."""
    from .summaries import _identity

    person = _manual_contact(name, title, email, profile_url, phone=phone)
    identity = _identity(connection, candidate_id)
    packet = {"candidate_id": candidate_id, "company_key": identity["company_key"],
              "company_name": identity["label_hint"] or identity["company_key"],
              "contact_recommendations": {"people": [person]}}
    return save(connection, packet, person_key(identity["company_key"], person), _operator=True)


def set_contact_method(connection, pid, contact_method, *, append=False):
    """Explicit operator-reported channel; never inferred from contact availability."""
    if contact_method not in CONTACT_METHODS or (append and not contact_method):
        raise ValueError("Unknown contact method")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise LookupError("Saved person not found")
        require_allowed(row["company_key"])
        candidate = connection.execute(
            "SELECT hostname FROM candidates WHERE id=?", (row["candidate_id"],)
        ).fetchone()
        if candidate is None:
            raise LookupError("Company no longer exists")
        require_allowed(candidate[0])
        if append or row["contact_method"] != contact_method:
            stamp = _stamp()
            if contact_method:
                connection.execute(
                    "INSERT INTO outbound_contact_touchpoints"
                    "(person_id,contact_method,contacted_at) "
                    "VALUES(?,?,?)", (pid, contact_method, stamp))
            connection.execute(
                "UPDATE outbound_people SET contact_method=?,updated_at=? WHERE id=?",
                (contact_method, stamp, pid),
            )
        result = _item(connection.execute(
            "SELECT * FROM outbound_people WHERE id=?", (pid,)
        ).fetchone(), connection)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def _manual_contact(name, title, email, profile_url, phone=None):
    from ..contact_intelligence import normalize_phone
    from .models import validate_source_url

    name, title = name.strip(), title.strip()
    email, profile_url = email.strip(), profile_url.strip()
    phone = (phone or "").strip()
    if phone:
        normalized = normalize_phone(phone) if re.fullmatch(r"\+?[0-9 ().-]{7,40}", phone) else None
        if not normalized:
            raise ValueError("Enter a valid work phone number")
        phone = normalized[1]
    if not name and not email and not phone:
        raise ValueError("Enter a name, email, or work phone")
    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email)):
        raise ValueError("Enter a valid email address")
    if profile_url:
        if "://" not in profile_url:
            profile_url = "https://" + profile_url
        profile_url = validate_source_url(profile_url).rstrip("/")
    if len(name) > 200 or len(title) > 200 or len(profile_url) > 2048:
        raise ValueError("Contact field is too long")
    return {"name": name or email or phone, "title": title, "email": email or None,
              "phone": phone or None,
              "profile_url": profile_url or None, "source_kind": "operator_supplied",
              "observed_at": _stamp()}


def person_details(connection, pid, details=None):
    """Edit saved display fields without rewriting source observations or identity."""
    connection.execute("BEGIN IMMEDIATE" if details is not None else "BEGIN")
    try:
        row = connection.execute("SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise LookupError("Prospect no longer exists")
        require_allowed(row["company_key"])
        candidate = connection.execute("SELECT hostname FROM candidates WHERE id=?",
                                       (row["candidate_id"],)).fetchone()
        if candidate is None:
            raise LookupError("Company no longer exists")
        require_allowed(candidate[0])
        if details is not None:
            contact = json.loads(row["contact_json"])
            previous_phone = _effective_phone(contact)
            phone_supplied = details.get("phone") is not None
            person = _manual_contact(**{**details, "phone": details["phone"] if phone_supplied
                                        else previous_phone})
            conflict = connection.execute(
                """SELECT id FROM outbound_people WHERE company_key=? AND id<>?
                AND ((? IS NOT NULL AND email=? COLLATE NOCASE)
                OR (? IS NOT NULL AND rtrim(lower(profile_url),'/')=rtrim(lower(?),'/')))""",
                (row["company_key"], pid, person["email"], person["email"],
                 person["profile_url"], person["profile_url"])).fetchone()
            if conflict:
                raise ValueError("Another saved prospect already uses this email or profile")
            contact.setdefault("operator_edit_history", []).append({
                "name": row["name"], "title": row["title"], "email": row["email"],
                "phone": previous_phone, "profile_url": row["profile_url"], "changed_at": _stamp()})
            updates = contact.setdefault("operator_updates", {})
            updates.update({k: person[k] for k in ("name", "title", "email", "profile_url")})
            if phone_supplied and person["phone"] != previous_phone:
                updates["phone"] = person["phone"]
            connection.execute(
                """UPDATE outbound_people SET name=?,name_key=?,title=?,email=?,profile_url=?,
                contact_json=?,updated_at=? WHERE id=?""",
                (person["name"], identity_text(person["name"]), person["title"], person["email"],
                 person["profile_url"], json.dumps(contact), _stamp(), pid))
        result = _item(connection.execute(
            "SELECT * FROM outbound_people WHERE id=?", (pid,)).fetchone(), connection)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
