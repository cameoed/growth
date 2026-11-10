"""Resumable, secure removal of residual disallowed research references."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zlib

from .domain_policy import DOMAIN_POLICY_REVISION, disallowed_domains
from .sales_engine.evidence_accounting import _STORAGE_TAG, decode_record
from .storage_json import PACKET_TAG, decode_packet, decode_text, encode_packet

logger = logging.getLogger(__name__)


class BlockedReferences:
    """Find suffixes first, then expand their prefix once; no repeated suffix walks."""

    def __init__(self, roots):
        alternatives = "|".join(re.escape(root) for root in roots)
        self.suffixes = re.compile(
            r"(?<![a-z0-9-])(?:" + alternatives + r")(?:\.(?=[^a-z0-9.-]|$))?(?![a-z0-9.-])",
            re.IGNORECASE,
        )
        self.label = re.compile(r"[a-z0-9-]", re.IGNORECASE)

    def search(self, text):
        return self.suffixes.search(text)

    def sub(self, replacement, text):
        parts, cursor = [], 0
        for match in self.suffixes.finditer(text):
            start = match.start()
            while start > cursor and text[start - 1] == ".":
                before = start - 1
                while before > cursor and self.label.fullmatch(text[before - 1]):
                    before -= 1
                if before == start - 1:
                    break
                start = before
            parts.extend((text[cursor:start], replacement))
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts)


def blocked_reference_pattern():
    return BlockedReferences(disallowed_domains())


class JSONStringDecoder:
    """Stream JSON string escapes, retaining escape parity across chunk boundaries."""

    def __init__(self):
        self.quoted = False
        self.escape = False
        self.digits = None

    def feed(self, text):
        result = []
        for char in text:
            if self.digits is not None:
                self.digits += char
                if len(self.digits) == 4:
                    try:
                        result.append(chr(int(self.digits, 16)))
                    except ValueError:
                        result.append("\\u" + self.digits)
                    self.digits = None
            elif self.escape:
                self.escape = False
                if char == "u":
                    self.digits = ""
                else:
                    result.append(
                        {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(char, char)
                    )
            elif char == "\\" and self.quoted:
                self.escape = True
            else:
                result.append(char)
                if char == '"':
                    self.quoted = not self.quoted
        return "".join(result)


def _scrub_json(value, pattern):
    if isinstance(value, str):
        return pattern.sub("[removed-domain]", value)
    if isinstance(value, list):
        return [_scrub_json(item, pattern) for item in value]
    if isinstance(value, dict):
        return {
            pattern.sub("[removed-domain]", key): _scrub_json(item, pattern)
            for key, item in value.items()
        }
    return value


def scrub_value(value, pattern, *, roots=None):
    """Decode compressed ledgers and JSON escapes before checking for references."""
    if isinstance(value, bytes) and value.startswith(PACKET_TAG):
        records = json.loads(decode_packet(value))
        cleaned = [[scrub_value(cell, pattern, roots=roots) for cell in row] for row in records]
        if cleaned == records:
            return value
        return encode_packet(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
    original = decode_text(value)
    if original != value:
        cleaned = scrub_value(original, pattern, roots=roots)
        # Cleanup can change projected fields; keep the complete cleaned JSON as
        # plain text until the next normal write/offline compression chooses its profile.
        return cleaned if cleaned != original else value
    packed = isinstance(value, bytes) and value.startswith(_STORAGE_TAG)
    if packed:
        text = decode_record(value)
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    else:
        return value
    # JSON can hide hostname characters only through Unicode escapes. Other JSON
    # escapes do not conceal domain letters/dots. This is an exact rejection, not
    # sampling: compressed ledgers are always decoded first. The four Unicode
    # characters matching ASCII letters under re.IGNORECASE retain the full regex.
    if roots is not None and len(text) > 1000 and not any(letter in text for letter in "İıſK"):
        lowered = text.lower()
        possible = any(root in lowered for root in roots)
    else:
        possible = bool(pattern.search(text))
    if not possible and "\\u" not in text:
        return value
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        cleaned = pattern.sub("[removed-domain]", text)
    else:
        cleaned_value = _scrub_json(parsed, pattern)
        if cleaned_value == parsed:
            return value
        cleaned = json.dumps(cleaned_value, ensure_ascii=False, separators=(",", ":"))
    if cleaned == text:
        return value
    if packed:
        return _STORAGE_TAG + zlib.compress(cleaned.encode(), level=6)
    return cleaned.encode() if isinstance(value, bytes) else cleaned


def purge_orphan_accounting(connection):
    """Remove rebuildable accounting whose owning score publication no longer exists."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    accounting_tables = [name for name in (
        "engine_evidence_accounting", "engine_evidence_accounting_legacy", "engine_evidence_packets"
    ) if name in tables]
    if not accounting_tables or "engine_company_ranking" not in tables:
        return 0
    connection.execute("PRAGMA secure_delete=ON")
    with connection:
        removed = 0
        for table in accounting_tables:
            logical_count = connection.execute(
                f"SELECT COALESCE(SUM(record_count),0) FROM {table} WHERE company_key NOT IN "
                "(SELECT company_key FROM engine_company_ranking)"
            ).fetchone()[0] if table == "engine_evidence_packets" else None
            count = connection.execute(
                f"DELETE FROM {table} WHERE company_key NOT IN "
                "(SELECT company_key FROM engine_company_ranking)"
            ).rowcount
            removed += count if logical_count is None else logical_count
        if removed and "engine_read_index_state" in tables:
            connection.execute(
                "UPDATE engine_read_index_state SET generation=generation+1 WHERE id=1"
            )
    return removed


def clean_residual_references(connection, *, batch_size=500, progress=lambda value: None):
    """Clean all live text/JSON/decoded ledger cells, keeping unrelated records.

    Transactions contain bounded batches and restore immutable triggers before commit.
    Scoring/read invalidation triggers remain installed throughout. A crash can resume
    by rerunning: completed cells no longer match and original triggers are never lost.
    """
    pattern = blocked_reference_pattern()
    roots = disallowed_domains()
    orphaned = purge_orphan_accounting(connection)
    logger.info("Removed %s orphaned derived accounting rows", orphaned)
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    tables = [
        (r[1], r[2])
        for r in connection.execute("PRAGMA table_list")
        if r[0] == "main"
        and r[2] in {"table", "virtual"}
        and not r[1].startswith("sqlite_")
        and r[1] != "schema_migrations"
    ]
    counts = {}
    for table, kind in sorted(tables):
        columns = [
            r[1]
            for r in connection.execute(f'PRAGMA table_info("{table}")')
            if any(t in r[2].upper() for t in ("TEXT", "BLOB", "CHAR"))
        ]
        if not columns:
            continue
        projection = ",".join(f'"{c}"' for c in columns)
        cursor = 0
        changed = 0
        while True:
            rows = connection.execute(
                f'SELECT rowid,{projection} FROM "{table}" WHERE rowid>? ORDER BY rowid LIMIT ?',
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                cells = [
                    (column, scrub_value(value, pattern, roots=roots))
                    for column, value in zip(columns, row[1:], strict=True)
                ]
                changed_cells = [
                    (column, value)
                    for (column, value), old in zip(cells, row[1:], strict=True)
                    if value != old
                ]
                if changed_cells:
                    updates.append((row[0], changed_cells))
            if updates:
                with connection:
                    immutable = connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
                        "AND upper(sql) LIKE '%RAISE(%'",
                        (table,),
                    ).fetchall()
                    for name, _ in immutable:
                        connection.execute(f'DROP TRIGGER "{name}"')
                    for rowid, cells in updates:
                        if table == "source_crawl_pages":
                            # Rebuildable parsed receipts must not retain blocked-company payloads.
                            connection.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
                            continue
                        assignments = ",".join(f'"{c}"=?' for c, _ in cells)
                        connection.execute(
                            f'UPDATE "{table}" SET {assignments} WHERE rowid=?',
                            [*[v for _, v in cells], rowid],
                        )
                    for _, sql in immutable:
                        connection.execute(sql)
                changed += len(updates)
            cursor = rows[-1][0]
        if kind == "virtual" and table in {
            "engine_company_search",
            "engine_candidate_search",
            "engine_mission_search",
        }:
            with connection:
                connection.execute(f'INSERT INTO "{table}"("{table}") VALUES(\'optimize\')')
        counts[table] = changed
        logger.info("Residual audit checked %s; cleaned %s rows", table, changed)
        progress({"table": table, "rows_cleaned": changed})
    fingerprint = hashlib.sha256(
        (DOMAIN_POLICY_REVISION + "\n" + "\n".join(disallowed_domains())).encode()
    ).hexdigest()
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO schema_migrations(name,applied_at,rows_changed) "
            "VALUES (?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
            (f"domain-residual-cleanup:{fingerprint}", sum(counts.values())),
        )
    return {
        "rows_cleaned": sum(counts.values()),
        "orphaned_accounting_removed": orphaned,
        "tables_changed": {k: v for k, v in counts.items() if v},
    }


def clean_generated_research(project_root):
    """Remove stale generated reports/cohorts; never touch application source files."""
    from pathlib import Path

    pattern = blocked_reference_pattern()
    removed = 0
    for directory in ("reports", "artifacts"):
        root = Path(project_root) / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            extensions = {".json", ".jsonl", ".md", ".csv", ".tsv", ".html", ".txt", ".log"}
            if path.suffix.lower() not in extensions:
                continue
            tail = ""
            decoded_tail = ""
            decoder = JSONStringDecoder() if path.suffix.lower() in {".json", ".jsonl"} else None
            matched = False
            with path.open(errors="replace") as stream:
                while chunk := stream.read(1024 * 1024):
                    text = tail + chunk
                    if pattern.search(text):
                        matched = True
                        break
                    tail = text[-512:]
                    if decoder is not None:
                        decoded = decoded_tail + decoder.feed(chunk)
                        if pattern.search(decoded):
                            matched = True
                            break
                        decoded_tail = decoded[-512:]
            if matched:
                path.unlink()
                removed += 1
    return removed
