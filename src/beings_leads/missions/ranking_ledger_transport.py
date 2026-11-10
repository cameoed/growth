"""Private SSH transport for complete, pinned mission accounting ledgers."""

import base64
import gzip
import json
import subprocess
import sys
from pathlib import Path

from ..oracle_deployment import REMOTE_DB, REMOTE_ROOT, REMOTE_RUNTIME_ENV, Remote
from ..storage_json import decode_text
from ..store import LeadStore
from .scoring import _ledger_metadata, _ledger_rows

VERSION = 1
MAX_COMPANIES = 128
MAX_REQUEST_BYTES = 64 * 1024


def _wire_value(value):
    return [base64.b64encode(value).decode("ascii")] if isinstance(value, bytes) else value


class LedgerRow(tuple):
    """Retain SQLite column order and first-column resolution for duplicate names."""

    def __new__(cls, columns, values):
        row = super().__new__(cls, (
            base64.b64decode(value[0], validate=True) if isinstance(value, list)
            else decode_text(value) if column.lower().endswith("_json")
            else value
            for column, value in zip(columns, values, strict=True)
        ))
        row.columns = columns
        return row

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        return super().__getitem__(self.columns.index(key) if isinstance(key, str) else key)


def _validate(request):
    if not isinstance(request, dict) or request.get("version") != VERSION:
        raise ValueError("Unsupported ranking ledger protocol")
    companies = request.get("companies")
    if not isinstance(companies, list) or not 1 <= len(companies) <= MAX_COMPANIES:
        raise ValueError("Invalid ranking ledger batch")
    for pin in companies:
        if (
            not isinstance(pin, list) or len(pin) != 3
            or not isinstance(pin[0], str) or not 1 <= len(pin[0]) <= 253
            or type(pin[1]) is not int or not isinstance(pin[2], str)
            or len(pin[2]) > 256
        ):
            raise ValueError("Invalid ranking ledger pin")
    if len({pin[0] for pin in companies}) != len(companies):
        raise ValueError("Duplicate ranking ledger company")
    return companies


def write_batch(store, request, output):
    """One SQLite read snapshot. Stream physical rows without materializing the batch."""
    companies = _validate(request)
    connection = store.connection
    if not connection.execute("PRAGMA query_only").fetchone()[0]:
        raise ValueError("Ranking ledger transport requires a read-only store")

    def emit(*values):
        output.write(json.dumps(values, separators=(",", ":")).encode() + b"\n")

    connection.execute("BEGIN")
    try:
        ledgers, columns = _ledger_metadata(store)
        emit("version", VERSION)
        for company, candidate, input_version in companies:
            pin = connection.execute(
                "SELECT candidate_id,input_version FROM engine_mission_inventory "
                "WHERE company_key=?", (company,),
            ).fetchone()
            if pin is None or tuple(pin) != (candidate, input_version):
                emit("stale", company)
                continue
            emit("company", company)
            for table, role in ledgers:
                cursor = _ledger_rows(connection, table, columns[table], company, raw=True)
                names = [column[0] for column in cursor.description] if cursor else []
                emit("table", table, role, names)
                for row in cursor:
                    emit("row", *map(_wire_value, row))
            emit("end", company)
            output.flush()
        emit("done")
    finally:
        connection.rollback()


def read_batch(stream, companies):
    """Yield only complete company ledgers in request order."""
    def receive():
        line = stream.readline()
        if not line:
            raise EOFError("Incomplete ranking ledger response")
        return json.loads(line)

    if receive() != ["version", VERSION]:
        raise ValueError("Unsupported ranking ledger response")
    for company, _, _ in companies:
        frame = receive()
        if frame == ["stale", company]:
            yield company, None
            continue
        if frame != ["company", company]:
            raise ValueError("Unexpected ranking ledger company")
        ledgers = []
        frame = receive()
        while frame[0] == "table":
            _, table, role, names = frame
            rows = []
            frame = receive()
            while frame[0] == "row":
                rows.append(LedgerRow(names, frame[1:]))
                frame = receive()
            ledgers.append((table, role, rows))
        if frame != ["end", company]:
            raise ValueError("Incomplete company ledger")
        yield company, ledgers
    if receive() != ["done"] or stream.read(1):
        raise ValueError("Unexpected ranking ledger response")


def fetch(remote: Remote, companies):
    """Use the authenticated operator SSH channel. There is no HTTP route."""
    companies = [list(pin) for pin in companies]
    request = {"version": VERSION, "companies": companies}
    _validate(request)
    payload = json.dumps(request).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("Ranking ledger request exceeds its byte limit")
    command = (
        f"sudo -n -u beings-growth {REMOTE_RUNTIME_ENV} "
        f"{REMOTE_ROOT}/.venv/bin/python -m beings_leads.missions.ranking_ledger_transport"
    )
    with subprocess.Popen(
        remote.ssh(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    ) as process:
        try:
            process.stdin.write(payload)
            process.stdin.close()
            with gzip.GzipFile(fileobj=process.stdout) as stream:
                yield from read_batch(stream, companies)
            if process.wait() != 0:
                raise RuntimeError("Oracle ranking ledger request failed")
        finally:
            if process.poll() is None:
                process.terminate()


def main():
    request = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(request) > MAX_REQUEST_BYTES:
        raise ValueError("Ranking ledger request exceeds its byte limit")
    with (
        LeadStore(Path(REMOTE_DB), initialize=False, read_only=True) as store,
        gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", compresslevel=1) as output,
    ):
        write_batch(store, json.loads(request), output)


if __name__ == "__main__":
    main()
