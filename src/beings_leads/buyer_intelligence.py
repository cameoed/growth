"""Attributable buyer evidence for the shared Sales score; never buyer confirmation.

All parsers are local. Partial samples establish positive observations only, never absence,
spend, unused capacity, budget authority or company-wide production totals.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .creative_capacity import BuyingSignalEvent
from .evidence_policy import DURABLE_HORIZON_DAYS
from .evidence_policy import VERSION as FRESHNESS_VERSION
from .sales_engine.models import normalize_company_key, validate_source_url

VERSION = "buyer-evidence-v1"
MAX_ROWS = 200


def date_value(value):
    if isinstance(value, dict):
        value = value.get("date") or value.get("timestamp")
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else None
        text = str(value or "").strip()
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def url(value):
    try:
        return validate_source_url(str(value or ""))
    except (ValueError, TypeError):
        return ""


def same_domain(value, company):
    try:
        host = (
            (urlsplit(value if "://" in str(value) else "https://" + str(value)).hostname or "")
            .lower()
            .removeprefix("www.")
        )
        return host == company or host.endswith("." + company)
    except ValueError:
        return False


def canonical_asset(value):
    """Strip delivery parameters; preserve path identity, never guess visual similarity."""
    valid = url(value)
    if not valid:
        return ""
    parts = urlsplit(valid)
    return urlunsplit(
        ("https", parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/"), "", "")
    )


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


class BuyerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_key: str
    kind: Literal["advertising", "purchase_request", "workforce", "leadership", "creator"]
    record_key: str = Field(min_length=1, max_length=300)
    source_url: str
    provider: str = Field(max_length=120)
    observed_at: datetime
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    identity_verified: bool = False
    identity_basis: str = Field(default="", max_length=300)
    coverage: Literal["partial", "complete", "unknown"] = "partial"
    current: bool = True
    excerpt: str = Field(default="", max_length=6000)
    data: dict = Field(default_factory=dict)
    raw_record: dict = Field(default_factory=dict)

    _company = field_validator("company_key")(normalize_company_key)
    _url = field_validator("source_url")(validate_source_url)

    @field_validator("observed_at", "effective_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evidence dates must include a timezone")
        return value.astimezone(UTC) if value else value


def _record(
    company,
    kind,
    source,
    observed,
    *,
    effective=None,
    key=None,
    provider="public_web",
    verified=False,
    basis="",
    data=None,
    excerpt="",
    current=True,
    expires=None,
    raw=None,
):
    if not url(source):
        return None
    return BuyerEvidence(
        company_key=company,
        kind=kind,
        record_key=key or f"{kind}:{canonical_asset(source)}",
        provider=provider,
        source_url=source,
        observed_at=observed,
        effective_at=effective,
        expires_at=expires,
        identity_verified=verified,
        identity_basis=basis,
        current=current,
        data=data or {},
        raw_record=raw or {},
        excerpt=excerpt[:6000],
    )


def rows(payload, keys):
    """Known provider wrappers only; do not recursively treat arbitrary objects as evidence."""
    node = payload
    for _ in range(4):
        if not isinstance(node, dict):
            return []
        for key in keys:
            if isinstance(node.get(key), list):
                return [r for r in node[key][:MAX_ROWS] if isinstance(r, dict)]
        if isinstance(node.get("element"), dict):
            return [node["element"]]
        node = node.get("data")
    return []


def parse_ads(payload, company, *, platform, observed_at, company_profile=""):
    result = []
    for row in rows(payload, ("ads", "elements")):
        variants = row.get("variants") or [row]
        for variant in variants[:10]:
            if not isinstance(variant, dict):
                continue
            content = variant.get("content") or {}
            content = content if isinstance(content, dict) else {}
            advertiser = variant.get("advertiser") or {}
            advertiser = advertiser if isinstance(advertiser, dict) else {}
            about = row.get("about") or {}
            landing = (
                row.get("domain")
                or row.get("destinationUrl")
                or row.get("landingPage")
                or content.get("targetUrl")
                or content.get("pageUrl")
                or ""
            )
            profile = advertiser.get("linkedinUrl") or about.get("advertiserUrl") or ""
            verified = same_domain(landing, company) or bool(
                company_profile and canonical_asset(profile) == canonical_asset(company_profile)
            )
            source = row.get("adUrl") or row.get("url") or ""
            if not source and platform == "linkedin" and str(row.get("id", "")).isdigit():
                source = f"https://www.linkedin.com/ad-library/detail/{row['id']}"
            first = date_value(
                row.get("firstShown") or row.get("startDate") or about.get("startDate")
            )
            last = date_value(row.get("lastShown") or row.get("endDate") or about.get("endDate"))
            raw_format = str(
                row.get("format")
                or row.get("adType")
                or variant.get("creativeType")
                or about.get("format")
                or ""
            ).lower()
            video = row.get("video") or row.get("organicVideo") or content.get("videoSource")
            image = row.get("imageUrl") or row.get("image") or content.get("imageUrl")
            # Text search ads may have a rendered image. Format outranks that thumbnail.
            fmt = (
                "text"
                if "text" in raw_format or "search" in raw_format
                else "video"
                if video or "video" in raw_format
                else "carousel"
                if row.get("carouselImages") or "carousel" in raw_format
                else "image"
                if image or "image" in raw_format
                else "unknown"
            )
            assets = [image] if isinstance(image, str) else []
            if isinstance(video, str):
                assets.append(video)
            elif isinstance(video, list):
                assets.extend(v.get("src") for v in video if isinstance(v, dict))
            assets.extend(
                v if isinstance(v, str) else v.get("url")
                for v in row.get("carouselImages", [])
                if isinstance(v, (str, dict))
            )
            assets = sorted({canonical_asset(a) for a in assets if canonical_asset(a)})
            creative = str(row.get("creativeId") or row.get("id") or "")
            item = _record(
                company,
                "advertising",
                source,
                observed_at,
                effective=first,
                key=f"ad:{platform}:{creative}",
                provider=platform + "_ad_library",
                verified=verified,
                basis="advertiser_domain_or_first_party_profile" if verified else "",
                data={
                    "platform": platform,
                    "format": fmt,
                    "creative_id": creative,
                    "asset_keys": assets,
                    "first_shown": first.isoformat() if first else None,
                    "last_shown": last.isoformat() if last else None,
                    "asset_fingerprint": fingerprint(assets) if assets else "",
                },
                excerpt=str(row.get("description") or content.get("description") or ""),
                raw=row,
            )
            if item and creative:
                result.append(item)
    return result


_CREATIVE = re.compile(
    r"\b(?:creative|video|design|animation|motion|ugc|photograph\w*|content production)\b", re.I
)
_REQUEST = re.compile(
    r"\b(?:we(?:'re| are) (?:looking for|seeking)|(?:i(?:'m| am)|we) (?:need|seek)|"
    r"looking for (?:an? |our next )?(?:creative |video |design )?"
    r"(?:agency|freelancer|production partner)|"
    r"request for proposals?|rfp|seeking (?:an? )?(?:agency|production partner)|"
    r"recommend (?:an? )?(?:agency|video|creative)|replacing our (?:agency|creative partner))\b",
    re.I,
)
_SUPPLIER = re.compile(
    r"\b(?:we (?:offer|provide|help brands)|our (?:clients|services)|"
    r"hire me|available for hire|looking for clients|agency services|"
    r"how to (?:find|hire|choose)|tips for|example rfp|rfp template)\b",
    re.I,
)
_RECURRING = re.compile(
    r"\b(?:monthly|weekly|ongoing|retainer|recurring|every month|always.on)\b", re.I
)
_CLOSED = re.compile(
    r"\b(?:closed|awarded|cancelled|canceled|no longer seeking|partner selected|filled)\b", re.I
)
_ROLE = (
    r"(?:chief marketing officer|CMO|head of (?:growth|marketing|brand|creative)|"
    r"(?:brand|creative|marketing|growth) (?:director|lead|vice president|vp)|"
    r"VP of (?:marketing|growth))"
)
_APPOINT = re.compile(
    rf"\b(?:appoints?|appointed|promot(?:es|ed)|welcomes?|joins? as|joined as|named)\s+"
    rf"(?:(?P<person>(?-i:[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){{1,3}}))"
    rf"\s+(?:as |to |our new |as our new )?)?"
    rf"(?:the |new |our )?(?P<role>{_ROLE})\b",
    re.I,
)
_DEPARTURE = re.compile(
    r"\b(?:resigns?|resigned|departs?|stepped down|former|interim vacancy)\b", re.I
)
_BUDGET = re.compile(
    r"(?:\b(?:USD|US\$)\s*|\$)([\d,]+(?:\.\d+)?)\s*(k)?\s*(?:/|per\s+|a\s+)(month|mo\b|year|annum)",
    re.I,
)


def parse_public_text(
    text,
    source,
    company,
    *,
    observed_at,
    published_at=None,
    provider="public_web",
    author_verified=False,
    author_name="",
    author_role="",
):
    """Only first-party purchase/appointment language or verified professional authors qualify."""
    text = re.sub(r"\s+", " ", str(text or ""))[:20000]
    first_party = same_domain(source, company)
    verified = first_party or author_verified
    stamp = date_value(published_at)
    result = []
    third_party_story = bool(
        re.search(
            r"\b(?:our client|case study|customer story|on behalf of|partner company)\b", text, re.I
        )
    )
    if third_party_story:
        return []
    if _CREATIVE.search(text) and _REQUEST.search(text) and not _SUPPLIER.search(text):
        # Employment ads do not establish a decision to purchase external production.
        outside = re.search(
            r"\b(?:agency|freelanc\w*|production partner|vendor|supplier|rfp|contractor)\b",
            text,
            re.I,
        )
        if outside:
            budget = _BUDGET.search(text)
            monthly = None
            if budget and not re.search(
                r"(?:CAD|AUD|NZD|SGD|HKD|salary|media spend|ad spend)", text, re.I
            ):
                amount = float(budget[1].replace(",", "")) * (1000 if budget[2] else 1)
                monthly = amount / 12 if budget[3].lower() in {"year", "annum"} else amount
            deadline_match = re.search(
                r"(?:deadline|due|closes?|submit by)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})", text, re.I
            )
            deadline = date_value(deadline_match[1]) + timedelta(days=1) if deadline_match else None
            item = _record(
                company,
                "purchase_request",
                source,
                observed_at,
                effective=stamp,
                provider=provider,
                verified=verified,
                basis="first_party_or_verified_author",
                current=not bool(_CLOSED.search(text)),
                expires=deadline,
                excerpt=text,
                data={
                    "recurring": bool(_RECURRING.search(text)),
                    "monthly_budget_usd": monthly,
                    "budget_basis": "published_service_scope",
                    "author_name": author_name,
                    "author_role": author_role,
                },
            )
            if item:
                result.append(item)
    appointment = _APPOINT.search(text)
    prefix = text[: appointment.start()].strip() if appointment else ""
    company_subject = (
        not prefix
        or bool(re.search(r"\bwe\b", prefix[-100:], re.I))
        or company.split(".")[0].replace("-", "").casefold()
        in re.sub(r"[^a-z0-9]", "", prefix[-100:].casefold())
        or author_verified
    )
    if appointment and company_subject and not _DEPARTURE.search(text):
        person = appointment.group("person") or author_name
        if person:
            item = _record(
                company,
                "leadership",
                source,
                observed_at,
                effective=stamp,
                provider=provider,
                verified=verified,
                basis="dated_first_party_or_verified_author",
                excerpt=text,
                data={
                    "person": person,
                    "role": appointment.group("role"),
                    "change": "promotion" if "promot" in appointment[0].lower() else "appointment",
                },
            )
            if item:
                result.append(item)
    return result


def parse_public_html(html, source, company, *, observed_at):
    from .html_document import parse_html

    soup = parse_html(html, mutable=True)
    stamp = None
    for node in soup.select(
        'meta[property="article:published_time"], meta[name="date"], time[datetime]'
    ):
        stamp = date_value(node.get("content") or node.get("datetime"))
        if stamp:
            break
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            raw = json.loads(script.string or script.get_text())
            nodes = raw if isinstance(raw, list) else [raw]
            for node in nodes:
                if isinstance(node, dict):
                    for value in [node, *(node.get("@graph") or [])]:
                        if isinstance(value, dict) and value.get("datePublished"):
                            stamp = date_value(value["datePublished"]) or stamp
        except (ValueError, TypeError):
            continue
    for node in soup(["script", "style", "nav", "footer", "header", "template"]):
        node.decompose()
    body = soup.find("article") or soup.find("main") or soup
    result = parse_public_text(
        body.get_text(" ", strip=True),
        source,
        company,
        observed_at=observed_at,
        published_at=stamp,
    )
    # A first-party partnership attribution can seed an external creator lookup. The link
    # itself is research context: it has no post date and cannot add production points.
    if same_domain(source, company):
        for anchor in body.select("a[href]")[:200]:
            profile = url(anchor.get("href"))
            parts = urlsplit(profile)
            context = anchor.parent.get_text(" ", strip=True)[:1000]
            if (
                parts.hostname in {"www.tiktok.com", "tiktok.com"}
                and re.fullmatch(r"/@[\w.]+/?", parts.path)
                and re.search(r"\b(?:creator|influencer)\b", context, re.I)
                and re.search(
                    r"\b(?:partnered|collaborated|sponsored|partnership with)\b", context, re.I
                )
            ):
                item = _record(
                    company,
                    "creator",
                    source,
                    observed_at,
                    key="creator-discovery:" + canonical_asset(profile),
                    verified=True,
                    basis="first_party_external_creator_attribution",
                    excerpt=context,
                    data={"discovery_creator_profile": profile},
                )
                if item:
                    result.append(item)
    return result[:30]


def parse_posts(payload, company, *, observed_at, company_profile="", people=()):
    profiles = {
        canonical_asset(p.get("linkedin_url")): p
        for p in people
        if p.get("accepted") and same_domain(p.get("company_domain"), company)
    }
    result = []
    for row in rows(payload, ("elements", "posts")):
        author = row.get("author") or {}
        if not isinstance(author, dict):
            continue
        profile = canonical_asset(author.get("linkedinUrl"))
        person = profiles.get(profile) or {}
        corporate = bool(company_profile and profile == canonical_asset(company_profile))
        # Provider search filters are discovery, not identity proof. Reposts are another author.
        if row.get("repostedBy") or row.get("repostedPost") or row.get("isRepost"):
            continue
        result.extend(
            parse_public_text(
                row.get("content"),
                row.get("linkedinUrl") or row.get("url"),
                company,
                observed_at=observed_at,
                published_at=row.get("postedAt"),
                provider="linkedin_posts",
                author_verified=corporate or bool(person),
                author_name=author.get("name", ""),
                author_role=person.get("title", ""),
            )
        )
    return result


def parse_workforce(payload, company, *, observed_at):
    node = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(node, dict):
        return []
    verified = same_domain(node.get("domain"), company)
    samples = [node, *[r for r in node.get("history", [])[:24] if isinstance(r, dict)]]
    result = []
    for index, sample in enumerate(samples):
        stamp = date_value(sample.get("date")) or (observed_at if index == 0 else None)
        counts = sample.get("department_headcount")
        if not stamp or not isinstance(counts, dict):
            continue
        counts = {k: v for k, v in counts.items() if type(v) is int and v >= 0}
        total = sample.get("observed_employee_count")
        unknown = sample.get("unclassified_headcount")
        item = _record(
            company,
            "workforce",
            "https://api.companyenrich.com/companies/workforce?domain=" + company,
            observed_at,
            effective=stamp,
            key=f"workforce:companyenrich:{stamp.isoformat()}",
            provider="companyenrich",
            verified=verified,
            basis="provider_exact_domain",
            data={
                "department_counts": counts,
                "observed_employees": total,
                "unclassified": unknown,
                "scope": "observed_public_profiles",
                "definition": "companyenrich_root_departments_v1",
            },
            raw=sample,
        )
        if item:
            result.append(item)
    return result


def parse_instagram(payload, company, *, observed_at, handle):
    result = []
    for row in rows(payload, ("items",)):
        row = row.get("node", row)
        sponsor_list = row.get("sponsor_tags") or row.get("branded_content_tags") or []
        sponsors = []
        for sponsor in sponsor_list:
            if isinstance(sponsor, dict):
                sponsor = sponsor.get("sponsor") or sponsor.get("user") or sponsor
                sponsors.append(str(sponsor.get("username") or "").lower())
        caption = row.get("caption") or {}
        caption = caption.get("text", "") if isinstance(caption, dict) else str(caption)
        # Ordinary tagging/affiliate links alone never prove a sponsored relationship.
        disclosed = handle.lower() in sponsors
        user = row.get("user") or row.get("owner") or {}
        creator = str(user.get("username") or "")
        code = row.get("code") or row.get("shortcode")
        source = row.get("permalink") or (f"https://www.instagram.com/p/{code}/" if code else "")
        assets = [canonical_asset(source)] if url(source) else []
        media = [
            *(row.get("video_versions") or []),
            *((row.get("image_versions2") or {}).get("candidates") or []),
        ]
        assets.extend(
            canonical_asset(m.get("url"))
            for m in media
            if isinstance(m, dict) and canonical_asset(m.get("url"))
        )
        item = _record(
            company,
            "creator",
            source,
            observed_at,
            effective=date_value(row.get("taken_at") or row.get("taken_at_timestamp")),
            provider="instagram_partnerships",
            verified=disclosed and bool(creator) and creator.lower() != handle.lower(),
            basis="explicit_sponsor_first_party_handle",
            excerpt=caption,
            raw=row,
            data={
                "creator": creator,
                "sponsor": handle,
                "disclosure": "sponsor_tag" if disclosed else "unverified_tag",
                "asset_keys": assets,
                "cash_budget_known": False,
            },
        )
        if item:
            result.append(item)
    return result


def parse_tiktok(payload, company, *, observed_at, brand_names):
    result = []
    names = {n.strip().casefold() for n in brand_names if len(n.strip()) >= 4}
    for row in rows(payload, ("commercial_contents",)):
        sponsors = {str(n).strip().casefold() for n in row.get("brand_names", [])}
        creator = (row.get("creator") or {}).get("username") or ""
        for video in row.get("videos", [])[:10]:
            if not isinstance(video, dict):
                continue
            source = video.get("url", "")
            item = _record(
                company,
                "creator",
                source,
                observed_at,
                effective=date_value(row.get("create_timestamp") or row.get("create_date")),
                provider="tiktok_commercial_content",
                verified=bool(names & sponsors) and bool(creator),
                basis="company_linked_creator_and_exact_sponsor",
                current=video.get("status", "active") == "active",
                excerpt=str(row.get("label", "")),
                raw=row,
                data={
                    "creator": creator,
                    "sponsor": sorted(names & sponsors),
                    "disclosure": row.get("label"),
                    "asset_keys": [canonical_asset(source)],
                    "cash_budget_known": False,
                },
            )
            if item:
                result.append(item)
    return result


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS buyer_evidence (
        id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, kind TEXT NOT NULL,
        record_key TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS idx_buyer_evidence_company "
    "ON buyer_evidence(company_key,observed_at DESC,id DESC)",
)


def initialize_schema(connection):
    for statement in SCHEMA:
        connection.execute(statement)


def save_evidence(connection, evidence):
    count = 0
    for raw in evidence:
        item = BuyerEvidence.model_validate(raw)
        payload = item.model_dump_json()
        count += connection.execute(
            "INSERT OR IGNORE INTO buyer_evidence(company_key,kind,record_key,observed_at,"
            "payload_json,payload_hash) VALUES(?,?,?,?,?,?)",
            (
                item.company_key,
                item.kind,
                item.record_key,
                item.observed_at.isoformat(),
                payload,
                fingerprint(payload),
            ),
        ).rowcount
    return count


def load_evidence(connection, company, *, now):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = []
    if "buyer_evidence" in tables:
        # Resolve logical records in synthesis, never by an arbitrary row cutoff. Retain
        # withdrawals and workforce comparison history alongside advertising observations.
        for kind in ("advertising", "purchase_request", "workforce", "leadership", "creator"):
            for row in connection.execute(
                    "SELECT payload_json FROM buyer_evidence WHERE company_key=? AND kind=? "
                    "AND observed_at<=? "
                    "ORDER BY observed_at DESC,id DESC",
                    (company, kind, now.isoformat()),
                ):
                try:
                    result.append(json.loads(row[0]))
                except (ValueError, TypeError):
                    # Preserve and account for the malformed row without blocking other facts.
                    continue
    if "evidence_artifacts" in tables:
        # Reuse source-dated cached first-party publications immediately, at no research cost.
        for row in connection.execute(
            "SELECT canonical_url,excerpt,fetched_at,published_at FROM evidence_artifacts "
            "WHERE company_key=? AND fetched_at<=? AND fetched_at>=? "
            "AND published_at IS NOT NULL AND excerpt<>'' ORDER BY fetched_at DESC,id DESC",
            (company, now.isoformat(), (now - timedelta(days=90)).isoformat()),
        ):
            if same_domain(row[0], company):
                result.extend(
                    item.model_dump(mode="json")
                    for item in parse_public_text(
                        row[1],
                        row[0],
                        company,
                        observed_at=date_value(row[2]),
                        published_at=row[3],
                        provider="cached_first_party_publication",
                    )
                )
    return result


def _event(
    item,
    kind,
    family,
    strength,
    *,
    now,
    root,
    days=90,
    half_life=30,
    details=None,
    inferred=False,
    effective=None,
):
    stamp = effective or item.effective_at
    expiry = min(stamp + timedelta(days=days), item.expires_at or stamp + timedelta(days=days))
    return BuyingSignalEvent(
        event_key="buyer:" + fingerprint(item.company_key, kind, item.record_key),
        subject_key="domain:" + item.company_key,
        event_type=kind,
        family=family,
        polarity="positive",
        strength=strength,
        confidence="medium" if inferred else "high",
        coverage="partial",
        epistemic_status="inferred" if inferred else "observed",
        source_kind=item.provider,
        source_url=item.source_url,
        observed_at=item.observed_at,
        effective_at=stamp,
        expires_at=max(stamp, expiry),
        half_life_days=half_life,
        independence_key=root,
        summary=kind.replace("_", " ") + ".",
        details={k: json.dumps(v, sort_keys=True, default=str) for k, v in (details or {}).items()},
    )


def buyer_events(company, evidence, base_events=(), *, now, paid_asset_keys=()):
    """Capped causal contribution, not points per row. Dependencies never manufacture intent."""
    latest = {}
    for raw in evidence:
        try:
            item = BuyerEvidence.model_validate(raw)
        except (ValueError, TypeError):
            continue
        if item.company_key != company or item.observed_at > now:
            continue
        key = (item.kind, item.record_key)
        if key not in latest or (item.observed_at, not item.current) > (
            latest[key].observed_at,
            not latest[key].current,
        ):
            latest[key] = item
    current = [
        r
        for r in latest.values()
        if r.identity_verified
        and r.current
        and r.effective_at
        and now - timedelta(days=180) <= r.effective_at <= now
        and now - timedelta(days=90) <= r.observed_at <= now
        and (r.expires_at is None or r.expires_at > now)
    ]
    result = []
    ads = [
        r for r in current if r.kind == "advertising" and r.effective_at >= now - timedelta(days=90)
    ]
    # Same media asset across platforms has one production identity. Unknown media cannot
    # establish distinct production simply by having multiple ad IDs.
    visual = {}
    for item in sorted(ads, key=lambda r: r.effective_at):
        if item.data.get("format") in {"image", "video", "carousel"}:
            key = item.data.get("asset_fingerprint")
            if key:
                visual.setdefault(key, item)
    selected = list(visual.values())
    periods = {r.effective_at.strftime("%G-%V") for r in selected}
    span = (
        (max(r.effective_at for r in selected) - min(r.effective_at for r in selected)).days
        if selected
        else 0
    )
    recurring = len(selected) >= 3 and len(periods) >= 2 and span >= 14
    refs = [r.record_key for r in selected]
    if ads:
        item = max(ads, key=lambda r: r.effective_at)
        result.append(
            _event(
                item,
                "observed_cross_channel_advertising",
                "distribution_investment",
                60 if selected else 15,
                now=now,
                root="distribution:verified_ads",
                details={
                    "visual_creatives": len(selected),
                    "periods": len(periods),
                    "source_records": refs,
                    "spend_known": False,
                },
            )
        )
    if recurring:
        item = max(selected, key=lambda r: r.effective_at)
        result.append(
            _event(
                item,
                "observed_visual_creative_refresh",
                "recurring_workload",
                72,
                now=now,
                root="recurring:ad_history",
                details={
                    "source_records": refs,
                    "publication_periods": len(periods),
                    "span_days": span,
                    "distinct_creatives": len(selected),
                    "formats": sorted({r.data["format"] for r in selected}),
                    "refreshes_per_30_days_observed": round(
                        (len(selected) - 1) * 30 / max(span, 1), 2
                    ),
                },
            )
        )
    for item in current:
        if item.kind != "purchase_request" or item.effective_at < now - timedelta(days=90):
            continue
        budget = item.data.get("monthly_budget_usd")
        compatible = type(budget) in {int, float} and budget >= 5000
        incompatible = type(budget) in {int, float} and budget < 5000
        repeat = item.data.get("recurring") is True
        if incompatible:
            continue  # Explicit incompatible published scope cannot raise service fit or timing.
        details = {
            "source_records": [item.record_key],
            "recurring": repeat,
            "published_monthly_budget": budget,
            "budget_compatible": compatible,
            "buyer_confirmed": False,
        }
        root = "request:" + fingerprint(item.excerpt.casefold())
        result.append(
            _event(
                item,
                "explicit_external_creative_request",
                "production_pressure",
                88 if repeat else 52,
                now=now,
                root=root,
                days=90,
                details=details,
            )
        )
        result.append(
            _event(
                item,
                "current_creative_purchase_window",
                "commercial_timing",
                82 if repeat and compatible else 65 if repeat else 35,
                now=now,
                root=root,
                days=60,
                half_life=21,
                details=details,
            )
        )
        if repeat:
            result.append(
                _event(
                    item,
                    "requested_recurring_creative",
                    "recurring_workload",
                    78,
                    now=now,
                    root=root,
                    details=details,
                )
            )
    paid_keys = set(paid_asset_keys)
    paid_keys.update(a for r in ads for a in r.data.get("asset_keys", []))
    creators = {}
    for item in current:
        if item.kind != "creator" or item.effective_at < now - timedelta(days=90):
            continue
        assets = set(item.data.get("asset_keys") or [canonical_asset(item.source_url)])
        if assets & paid_keys:
            continue
        key = fingerprint(sorted(assets))
        creators.setdefault(key, item)
    if len(creators) >= 2:
        values = list(creators.values())
        creator_span = (
            max(r.effective_at for r in values) - min(r.effective_at for r in values)
        ).days
        if creator_span >= 14:
            item = max(values, key=lambda r: r.effective_at)
            details = {
                "source_records": [r.record_key for r in values],
                "distinct_creators": len({r.data.get("creator") for r in values}),
                "span_days": creator_span,
                "cash_budget_known": False,
                "unfilled_capacity_known": False,
            }
            result.append(
                _event(
                    item,
                    "observed_recurring_creator_partnerships",
                    "recurring_workload",
                    62,
                    now=now,
                    root="workload:external_creators",
                    details=details,
                )
            )
            result.append(
                _event(
                    item,
                    "observed_external_creative_relationship",
                    "proof_match",
                    38,
                    now=now,
                    root="workload:external_creators",
                    details=details,
                )
            )

    from .revenue_intelligence import decay_event

    demand = [
        r
        for r in [*base_events, *result]
        if r.family == "recurring_workload"
        and r.polarity == "positive"
        and decay_event(r, now=now).current_strength >= 30
    ]
    demand_refs = [r.event_key for r in demand]
    if demand:
        for item in current:
            if item.kind == "leadership" and item.effective_at >= now - timedelta(days=90):
                result.append(
                    _event(
                        item,
                        "marketing_responsibility_change",
                        "commercial_timing",
                        45,
                        now=now,
                        root="leadership:" + str(item.data.get("person", "")).lower(),
                        days=90,
                        half_life=30,
                        inferred=True,
                        details={
                            **item.data,
                            "demand_evidence": demand_refs,
                            "new_budget_known": False,
                        },
                    )
                )
        workforce = sorted(
            (r for r in current if r.kind == "workforce"), key=lambda r: r.effective_at
        )
        if workforce:
            item = workforce[-1]
            counts = item.data.get("department_counts") or {}
            marketing, design = counts.get("marketing"), counts.get("design")
            total, unknown = item.data.get("observed_employees"), item.data.get("unclassified")
            adequate = (
                type(total) is int
                and total >= 10
                and type(unknown) is int
                and 0 <= unknown <= total
                and sum(counts.values()) <= total
                and unknown / total <= 0.25
            )
            # Zero and missing observed design profiles are not evidence of no internal team.
            roles = item.data.get("observed_production_roles") or []
            small = type(design) is int and 1 <= design <= 5 and len(roles) <= 5
            growing = False
            previous = None
            for old in reversed(workforce[:-1]):
                age = (item.effective_at - old.effective_at).days
                prior_counts = old.data.get("department_counts") or {}
                prior = prior_counts.get("marketing")
                if (
                    30 <= age <= 120
                    and old.data.get("definition") == item.data.get("definition")
                    and type(prior) is int
                    and prior > 0
                    and type(marketing) is int
                ):
                    prior_total = old.data.get("observed_employees")
                    prior_unknown = old.data.get("unclassified")
                    if (
                        (
                            type(prior_total) is int
                            and prior_total > 0
                            and type(prior_unknown) is int
                            and abs(prior_unknown / prior_total - unknown / total) <= 0.1
                        )
                        if adequate
                        else False
                    ):
                        growing = marketing >= prior * 1.2 and marketing >= prior + 2
                        previous = old.record_key
                    break
            if (
                adequate
                and small
                and type(marketing) is int
                and marketing >= 5
                and item.effective_at >= now - timedelta(days=90)
            ):
                result.append(
                    _event(
                        item,
                        "observed_staffing_demand_pressure",
                        "production_pressure",
                        46 if growing else 30,
                        now=now,
                        root="capacity:observed_workforce",
                        inferred=True,
                        details={
                            "source_records": [item.record_key, previous],
                            "demand_evidence": demand_refs,
                            "marketing_growth_observed": growing,
                            "observed_design": design,
                            "observed_marketing": marketing,
                            "observed_production_roles": roles,
                            "external_capacity_unknown": True,
                        },
                    )
                )
    # Publish the earliest dependency boundary, so ranking caches cannot preserve an inference
    # after the supporting campaign, workforce sample or appointment has expired.
    sources = {r.record_key: r for r in current}
    all_events = {r.event_key: r for r in [*base_events, *result]}
    bounded = []
    for event in result:
        boundaries = [event.expires_at]
        for key in json.loads(event.details.get("source_records", "[]")):
            source = sources.get(key)
            if source:
                boundaries.extend(
                    [
                        source.effective_at + timedelta(days=90),
                        source.observed_at + timedelta(days=90),
                    ]
                )
                if source.expires_at:
                    boundaries.append(source.expires_at)
        for key in json.loads(event.details.get("demand_evidence", "[]")):
            dependency = all_events.get(key)
            if dependency:
                from .evidence_policy import next_dependency_boundary

                boundaries.append(next_dependency_boundary(dependency, now))
        expiry = min(b for b in boundaries if b is not None)
        if expiry > now:
            root = next(
                (
                    base.independence_key
                    for base in base_events
                    if base.family == event.family
                    and base.source_url == event.source_url
                    and base.independence_key
                ),
                event.independence_key,
            )
            bounded.append(
                event.model_copy(update={"expires_at": expiry, "independence_key": root})
            )
    # Historic creative cadence remains relevant between scans. It cannot establish active
    # advertising, a current purchase request, today's staffing gap or a buying window.
    historical = [r for r in latest.values() if r.identity_verified and r.current
                  and r.kind in {"advertising", "creator"} and r.effective_at
                  and now - timedelta(days=365) <= r.effective_at <= now
                  and now - timedelta(days=365) <= r.observed_at <= now]
    for kind in ("advertising", "creator"):
        distinct = {}
        for item in sorted(historical, key=lambda r: r.effective_at):
            if item.kind != kind:
                continue
            if kind == "advertising":
                if item.data.get("format") not in {"image", "video", "carousel"}:
                    continue
                asset = item.data.get("asset_fingerprint")
            else:
                assets = set(item.data.get("asset_keys") or [canonical_asset(item.source_url)])
                if assets & paid_keys:
                    continue
                asset = fingerprint(sorted(assets))
            if asset:
                distinct.setdefault(asset, item)
        values = list(distinct.values())
        if len(values) < (3 if kind == "advertising" else 2):
            continue
        first = min(r.effective_at for r in values)
        last = max(r.effective_at for r in values)
        if (last - first).days < 14:
            continue
        root = "recurring:ad_history" if kind == "advertising" else "workload:external_creators"
        if any(e.family == "recurring_workload" and e.independence_key == root for e in bounded):
            continue
        item = max(values, key=lambda r: r.effective_at)
        event = _event(
            item, "historical_visual_cadence" if kind == "advertising" else
            "historical_creator_cadence", "recurring_workload", 64 if kind == "advertising" else 56,
            now=now, root=root, days=DURABLE_HORIZON_DAYS,
            details={"source_records": [r.record_key for r in values],
                     "current_activity_known": False, "span_days": (last - first).days},
        )
        event = event.model_copy(update={
            "expires_at": min(min(r.observed_at, r.effective_at) for r in values)
            + timedelta(days=365),
            "details": {**event.details, "freshness_policy": FRESHNESS_VERSION},
        })
        if event.expires_at > now:
            bounded.append(event)
    return bounded
