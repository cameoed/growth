"""Read-time resolution of explicitly targeted fetch gaps; never edits diagnostic history."""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit


# Historical messages have no structured target. Fail closed unless one company and
# one complete URL accompany an unambiguous transport/HTTP failure.
@lru_cache(maxsize=512)
def fetch_target(detail: str) -> tuple[str, str]:
    text = detail.rsplit(" | ", 1)[-1]
    match = re.match(r"^([a-zA-Z0-9.-]+): (.+)$", text)
    if not match:
        return "", ""
    company, failure = match.groups()
    lower = failure.lower()
    if any(word in lower for word in (
        "parse", "xml", "feed", "rss", "atom", "robots", "security", "unsafe",
        "identity", "certificate", "refus", "policy", "microlink", "redirect",
    )):
        return "", ""
    if not re.search(
        r"\b(?:ReadTimeout|ConnectTimeout|ConnectError|ReadError|RemoteProtocolError|"
        r"TimeoutError|HTTPStatusError)\b|\breturned HTTP [45]\d\d\b", failure
    ):
        return "", ""
    urls = re.findall(r"https?://[^\s<>\"']+", failure)
    if len(urls) != 1:
        return "", ""
    url = urls[0].rstrip(":,;)")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")
        company = company.lower().rstrip(".").removeprefix("www.")
        if (host != company or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in {None, 80, 443}):
            return "", ""
    except ValueError:
        return "", ""
    return company, url


def configure(connection):
    connection.create_function("recovery_company", 1, lambda text: fetch_target(text)[0],
                               deterministic=True)
    connection.create_function("recovery_url", 1, lambda text: fetch_target(text)[1],
                               deterministic=True)


# The existing company/scanner/time artifact index bounds each lookup to one
# company's Microlink history after the failure. No inventory scan or JSON decoding.
UNRESOLVED = """
NOT EXISTS (
    SELECT 1 FROM evidence_artifacts a
    JOIN scanner_executions x ON x.id=a.execution_id
    WHERE a.company_key=recovery_company(smart_scan_errors.detail)
      AND a.scanner_id='microlink_hydration_v1'
      AND a.fetched_at > smart_scan_errors.timestamp
      AND julianday(a.fetched_at)>julianday(smart_scan_errors.timestamp)
      AND a.original_url=recovery_url(smart_scan_errors.detail)
      AND a.artifact_type IN ('microlink_contacts','microlink_company_pages',
                             'microlink_documents','microlink_technologies',
                             'microlink_media')
      AND x.company_key=a.company_key AND x.scanner_id=a.scanner_id
      AND x.status='success'
      AND EXISTS (
          SELECT 1 FROM atomic_observations o
          WHERE o.execution_id=a.execution_id AND o.company_key=a.company_key
            AND o.scanner_id=a.scanner_id AND o.predicate='microlink.page'
            AND o.source_url=a.canonical_url
      )
)
"""



def activity_signature(row):
    detail = str(row.get("detail") or "")
    message = str(row.get("message") or "")
    if " · " in message and not fetch_target(detail)[0]:
        detail = message.rsplit(" · ", 1)[-1] + ": " + detail
    return detail, str(row.get("timestamp") or "")


def resolved_activity(connection, entries):
    """Resolve bounded activity alongside the error ledger in its existing snapshot."""
    resolved = set()
    for row in entries:
        detail, timestamp = activity_signature(row)
        if fetch_target(detail)[0]:
            unresolved = connection.execute(
                "WITH smart_scan_errors(detail,timestamp) AS (VALUES (?,?)) "
                f"SELECT {UNRESOLVED} FROM smart_scan_errors", (detail, timestamp),
            ).fetchone()[0]
            if not unresolved:
                resolved.add((detail, timestamp))
    return resolved
