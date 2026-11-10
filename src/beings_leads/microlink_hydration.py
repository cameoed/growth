"""Explicit, bounded Microlink hydration over saved companies. No startup collection.

Each paid attempt owns a durable scanner receipt before dispatch. Page observations are
append-only supplements; search snippets never become company statements or contacts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .async_work import gather_owned, run_thread_owned
from .company_research import (
    PUBLIC_FETCH_SECONDS,
    CompanyResearchBudget,
    CompanyResearchDeferred,
    substantive_failure,
)
from .contact_intelligence import merge_contact_intelligence
from .domain_policy import is_disallowed, normalize_domain, require_allowed
from .inspect import MICROLINK_API_URL, UnsafeTargetError, _bounded_get, _ensure_public_host
from .models import (
    AtomicObservation,
    ContactIntelligence,
    EvidenceArtifact,
    ScannerCoverageManifest,
    ScannerExecutionSummary,
    SiteObservation,
)
from .provider_rate_limit import rate_limit_scope
from .rehydration import REHYDRATION_DAYS
from .scan_resources import LIMITS, async_resource, parse_weight
from .site_research import SiteResearchPlanner, company_host, purpose_for, research_url, url_key
from .store import LeadStore

SCANNER_ID = "microlink_hydration_v1"
VERSION = "1.0.0"
FEATURES = (
    "contacts", "company_pages", "web_search", "news_search", "documents",
    "technologies", "media", "business_listings",
)
MAX_BYTES = 750_000
PAGE_LIMIT = 8
COMPANY_CONCURRENCY = LIMITS["microlink_network"][1]
REFRESH_DAYS = REHYDRATION_DAYS
CONTACT_REFRESH_DAYS = REFRESH_DAYS
PAGE_EVIDENCE_DAYS = 30
DOCUMENT_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".rtf")


def is_allowed(value):
    return bool(normalize_domain(value)) and not is_disallowed(value)


class MicrolinkHydrationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contacts: bool = False
    company_pages: bool = False
    web_search: bool = False
    news_search: bool = False
    documents: bool = False
    technologies: bool = False
    media: bool = False
    business_listings: bool = False
    browser_interactions: bool = False
    company_limit: int = Field(default=100, ge=1, le=10_000)
    request_limit: int = Field(default=500, ge=1, le=45_000)

    @property
    def enabled(self):
        return any(getattr(self, name) for name in FEATURES)

    @model_validator(mode="after")
    def validate_interactions(self):
        if self.browser_interactions and not (self.contacts or self.company_pages):
            raise ValueError("Browser interactions require Contacts or Company pages")
        return self


class MicrolinkLimitReached(RuntimeError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _own_url(value, base, *, document=False):
    """Use the site's existing URL policy; documents are an explicit narrow extension."""
    if document:
        parsed = urlsplit(urljoin(base, str(value)))
        if not parsed.path.lower().endswith(DOCUMENT_SUFFIXES):
            return ""
        # Validate the original path with the same path/query/auth/host policy.
        surrogate = parsed._replace(path=parsed.path + "/document").geturl()
        if not research_url(surrogate, base):
            return ""
        value = parsed._replace(fragment="").geturl()
    else:
        value = research_url(str(value), base)
    return value if value and is_allowed(value) else ""


def select_cohort(store, limit, *, exclude_companies=()):
    """Rank the full saved inventory before limiting; keep canonical company identities."""
    rows = store.connection.execute(
        "SELECT c.id,c.hostname,c.source_apex FROM candidates c LEFT JOIN engine_company_ranking r "
        "ON r.candidate_id=c.id WHERE COALESCE(r.excluded,0)=0 AND EXISTS "
        "(SELECT 1 FROM snapshots s WHERE s.candidate_id=c.id) "
        "AND NOT EXISTS (SELECT 1 FROM prospect_workflow w "
        "WHERE w.candidate_id=c.id AND w.excluded=1) "
        "ORDER BY COALESCE(r.score,-1) DESC,c.discovery_rank DESC,c.id"
    )
    selected, seen = [], set(exclude_companies)
    for row in rows:
        if not is_allowed(row["hostname"]):
            continue
        key = store._candidate_company_keys([row["id"]]).get(row["id"])
        if not key or key in seen or not is_allowed(key):
            continue
        if store.connection.execute("SELECT target_is_suppressed(?,?,?)",
                                    (row["source_apex"], row["hostname"], key)).fetchone()[0]:
            continue
        saved = store.latest_snapshots([row["hostname"]]).get(row["hostname"])
        if not saved or not saved["observation"].live:
            continue
        seen.add(key)
        selected.append({"candidate_id": row["id"], "company_key": key,
                         "hostname": row["hostname"]})
        if len(selected) >= limit:
            break
    return selected


def saved_pages(connection, company, *, now, with_keys=False):
    """Latest attributable fact per URL, without refreshing its collection clock."""
    rows = connection.execute(
        "SELECT a.value_json,a.first_observed_at,a.expires_at,a.source_url,a.observation_key,"
        "a.metadata_json "
        "FROM atomic_observations a JOIN scanner_executions e ON e.id=a.execution_id "
        "WHERE a.company_key=? AND a.scanner_id=? AND a.predicate='microlink.page' "
        "AND a.first_observed_at<=? AND e.recorded_at<=? AND e.finished_at<=? "
        "AND a.validity_status='current' ORDER BY a.first_observed_at DESC,a.id DESC",
        (company, SCANNER_ID, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    seen, result = set(), []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        key = (url_key(row["source_url"]), metadata.get("feature", "page"))
        if key in seen:
            continue
        seen.add(key)
        if metadata.get("corrects_access_block"):
            continue
        if not row["expires_at"] or datetime.fromisoformat(row["expires_at"]) <= now:
            continue
        value = json.loads(row["value_json"])
        observation = SiteObservation.model_validate(value)
        if (observation.canonical_host != company or not is_allowed(row["source_url"])
                or company_host(row["source_url"]) != company):
            continue
        result.append((row["observation_key"], observation) if with_keys else observation)
    return result


def public_contacts(connection, company, original, *, now):
    # Visible contact reads select only exact JSON projections, never full page/research models.
    rows = connection.execute(
        "SELECT a.source_url,a.expires_at,a.metadata_json,"
        "json_extract(storage_json(a.value_json),'$.contacts') AS contacts_json,"
        "json_extract(storage_json(a.value_json),'$.canonical_host') AS owner "
        "FROM atomic_observations a JOIN scanner_executions e ON e.id=a.execution_id "
        "WHERE a.company_key=? AND a.scanner_id=? AND a.predicate='microlink.page' "
        "AND a.first_observed_at<=? AND e.recorded_at<=? AND e.finished_at<=? "
        "AND a.validity_status='current' ORDER BY a.first_observed_at DESC,a.id DESC",
        (company, SCANNER_ID, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    seen, contacts = set(), [ContactIntelligence.model_validate(original)]
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        key = (url_key(row["source_url"]), metadata.get("feature", "page"))
        if key in seen:
            continue
        seen.add(key)
        if (row["owner"] != company or not is_allowed(row["source_url"])
                or company_host(row["source_url"]) != company or not row["expires_at"]
                or datetime.fromisoformat(row["expires_at"]) <= now):
            continue
        contacts.append(ContactIntelligence.model_validate_json(row["contacts_json"] or "{}"))
    return merge_contact_intelligence(*contacts).model_dump(mode="json")


def future_boundaries(connection, company, *, now):
    rows = connection.execute(
        "SELECT a.first_observed_at,e.finished_at,e.recorded_at "
        "FROM atomic_observations a JOIN scanner_executions e ON e.id=a.execution_id "
        "WHERE a.company_key=? AND a.scanner_id=? AND a.predicate='microlink.page' "
        "AND (a.first_observed_at>? OR e.finished_at>? OR e.recorded_at>?)",
        (company, SCANNER_ID, now.isoformat(), now.isoformat(), now.isoformat()),
    )
    return tuple(stamp for row in rows for value in row if value
                 and (stamp := datetime.fromisoformat(value)) > now)


# Only public disclosure controls; never forms, sign-in, consent acceptance or arbitrary scripts.
BROWSER_FUNCTION = """async ({page}) => {
  await page.waitForSelector('body');
  await page.evaluate(() => {
    for (const node of Array.from(document.querySelectorAll('details:not([open])')).slice(0,4))
      node.open = true;
    const buttons = Array.from(document.querySelectorAll('button')).filter(node =>
      !node.closest('form') &&
      /^(load more|show more|read more|view team)$/i.test(node.innerText.trim()));
    for (const button of buttons.slice(0,2)) button.click();
  });
  await page.waitForNetworkIdle({idleTime:500,timeout:3000}).catch(() => {});
  return {url: await page.url(), html: await page.content()};
}"""


def page_parameters(url, feature, options):
    params = {"url": url, "meta": "false", "ttl": "1d", "staleTtl": "0"}
    if options.browser_interactions and feature in {"contacts", "company_pages"}:
        params.update({"function": BROWSER_FUNCTION, "prerender": "true"})
    else:
        params.update({"data.html.selector": "html", "data.html.attr": "html",
                       "prerender": "false" if feature == "documents" else "true"})
    if feature == "technologies":
        params.update({"insights.technologies": "true", "insights.lighthouse": "false",
                       "adblock": "false"})
    if feature == "media":
        params.update({"video": "true", "audio": "true"})
    return params


async def _parse(size, operation):
    async with (async_resource("parse_jobs"),
                async_resource("parse_bytes", weight=parse_weight(size)),
                async_resource("cpu")):
        return await run_thread_owned(operation)


class Hydrator:
    def __init__(self, database, run_id, options, api_key, client, checkpoint, progress,
                 *, db_lock=None, stopped=None, contact_refresh=False, force_contacts=False):
        self.contact_refresh = contact_refresh
        self.force_contacts = contact_refresh and force_contacts
        self.database, self.run_id, self.options = database, run_id, options
        self.api_key, self.client = api_key, client
        self.checkpoint, self.progress = checkpoint, progress
        self.robots = {}
        self.failures = 0
        self.db_lock = db_lock if db_lock is not None else asyncio.Lock()
        self.stopped = stopped if stopped is not None else asyncio.Event()

    async def db(self, operation):
        def execute():
            with LeadStore(self.database, initialize=False) as store:
                return operation(store)
        # One run owns reservations and writes. Count + claim must not race another
        # company worker, and cancelled writes must drain before releasing ownership.
        async with self.db_lock:
            return await run_thread_owned(execute)

    async def company_held(self, company):
        return await self.db(lambda store: CompanyResearchBudget(
            store.connection, company["company_key"], lane="microlink").held_until())

    async def company_failure(self, company, error):
        self.failures += 1
        if substantive_failure(error, paid=True):
            await self.db(lambda store: CompanyResearchBudget(
                store.connection, company["company_key"], lane="microlink").failure())

    async def company_success(self, company):
        await self.db(lambda store: CompanyResearchBudget(
            store.connection, company["company_key"], lane="microlink").success())

    @staticmethod
    def fail_pending(store, execution, reason, status="transient_error"):
        # Cancellation can arrive after an owned writer commits. Never overwrite that
        # result or replace CancelledError with a terminal-receipt validation exception.
        row = store.connection.execute(
            "SELECT status FROM scanner_executions WHERE id=?", (execution,),
        ).fetchone()
        if row and row[0] == "running":
            store.fail_scanner_execution(execution, error=reason, status=status)

    async def check_target(self, url, base):
        from .frontier_orchestration import _FrontierHttpFetcher

        if company_host(url) != company_host(base):
            raise UnsafeTargetError("Microlink target crossed company identity")
        require_allowed(url)
        await _ensure_public_host(urlsplit(url).hostname)
        fetcher = _FrontierHttpFetcher(
            self.client, SCANNER_ID, company_host(base), base, {url}, {}, {}, set(), set(),
            robots_cache=self.robots,
        )
        await fetcher._check_robots(url)

    def _reserve(self, store, company, params, feature):
        if self.stopped.is_set():
            raise MicrolinkLimitReached("Microlink phase stopped")
        CompanyResearchBudget(store.connection, company["company_key"], lane="microlink").check()
        version = "1." + _hash(params)[:24]
        previous = store.connection.execute(
            "SELECT id,status FROM scanner_executions WHERE company_key=? AND scanner_id=? "
            "AND scanner_version=? AND run_id=? ORDER BY id DESC LIMIT 1",
            (company["company_key"], SCANNER_ID, version, self.run_id),
        ).fetchone()
        if previous:
            # Even an interrupted reservation consumes this parent's ceiling. Never replay it.
            return None
        uncertain_since = (
            (datetime.now(UTC) - timedelta(days=CONTACT_REFRESH_DAYS)).isoformat()
            if self.contact_refresh else ""
        )
        uncertain = store.connection.execute(
            "SELECT 1 FROM scanner_executions WHERE company_key=? AND scanner_id=? "
            "AND scanner_version=? AND started_at>=? "
            "AND (status='running' OR error LIKE 'uncertain:%') LIMIT 1",
            (company["company_key"], SCANNER_ID, version, uncertain_since),
        ).fetchone()
        if uncertain:
            return None
        used = store.connection.execute(
            "SELECT COUNT(*) FROM scanner_executions WHERE run_id=? AND scanner_id=?",
            (self.run_id, SCANNER_ID),
        ).fetchone()[0]
        if used >= self.options.request_limit:
            raise MicrolinkLimitReached("Microlink request ceiling reached")
        config = {"feature": feature}
        if self.contact_refresh:
            config["contact_hydration"] = True
        claims = store.claim_scanner_work(
            [company["candidate_id"]], scanner_id=SCANNER_ID, scanner_version=version,
            run_id=self.run_id, extractor_version=VERSION, maximum=1,
            scope_key=_hash(params), config=config, independent_scope=True,
            manual_contact_refresh=self.force_contacts,
        )
        claimed = claims["claimed"].get(company["candidate_id"])
        return (claimed, version) if claimed else None

    async def request(self, company, params, feature):
        await self.checkpoint()
        reserved = await self.db(lambda store: self._reserve(store, company, params, feature))
        if reserved is None:
            return None
        execution, version = reserved
        try:
            async def provider_redirect(current, target):
                if (urlsplit(target).scheme != "https"
                        or urlsplit(target).hostname != urlsplit(MICROLINK_API_URL).hostname):
                    raise UnsafeTargetError("Microlink API redirect crossed provider identity")

            response, body = await _bounded_get(
                self.client, str(httpx.URL(MICROLINK_API_URL, params=params)),
                max_bytes=6 * MAX_BYTES + 250_000, accept="application/json",
                allowed_content_types=("application/json", "text/json"),
                request_headers={"x-api-key": self.api_key}, timeout=75,
                redirect_validator=provider_redirect,
                before_dispatch=self.checkpoint,
            )
            response.raise_for_status()
            if response.extensions.get("beings_body_truncated"):
                raise ValueError("Microlink response exceeded its byte ceiling")
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise ValueError("Microlink did not return a successful result")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("Microlink did not return structured data")
            observed_at = datetime.now(UTC)
            if "HIT" in response.headers.get("x-cache-status", "").upper():
                # With staleTtl=0 and ttl=1d, an undated cache hit is at most a day old.
                # Never relabel a provider's cached page as newly observed now.
                try:
                    age = min(86400, max(0, int(response.headers["age"])))
                except (KeyError, ValueError):
                    age = 86400
                observed_at -= timedelta(seconds=age)
            return execution, version, data, _hash(payload), len(body), observed_at
        except BaseException as exc:
            # Do not persist provider messages, request URLs or headers containing credentials.
            uncertain = not isinstance(exc, (ValueError, httpx.HTTPStatusError))
            reason = ("uncertain:" if uncertain else "failed:") + type(exc).__name__
            await self.db(lambda store: self.fail_pending(store, execution, reason))
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                raise MicrolinkLimitReached("Microlink provider quota or cooldown reached") from exc
            raise

    async def publish(self, company, receipt, feature, *, fragment=None, results=None, url="",
                      links=(), requested_url=""):
        execution, version, data, digest, size, now = receipt
        coverage = ScannerCoverageManifest(
            manifest_key=digest, scanner_id=SCANNER_ID, scanner_version=version,
            surface=feature, scope=url, attempted_units=1, successful_units=1, total_units=1,
            completeness="complete", absence_interpretable=False, observed_at=now,
            source_urls=[url] if url else [],
            limitations=["Selected public surface; not an exhaustive company census"],
        )
        artifact = EvidenceArtifact(
            artifact_key=digest, company_key=company["company_key"], scanner_id=SCANNER_ID,
            scanner_version=version, extractor_version=VERSION, canonical_url=url,
            original_url=requested_url or url, content_hash=digest, fetched_at=now, byte_count=size,
            robots_state="allowed" if fragment else "not_applicable",
            artifact_type="microlink_" + feature,
            metadata={"feature": feature, "provider": "microlink",
                      "provider_details": {key: data[key] for key in ("insights", "video", "audio")
                                           if key in data}},
        )
        evidence_days = CONTACT_REFRESH_DAYS if self.contact_refresh else PAGE_EVIDENCE_DAYS
        observation = AtomicObservation(
            observation_key=digest, company_key=company["company_key"], scanner_id=SCANNER_ID,
            scanner_version=version, extractor_version=VERSION, entity_key=company["company_key"],
            predicate="microlink.page" if fragment else "microlink.search_results",
            value=fragment.model_dump(mode="json") if fragment else results,
            source_url=url, confidence="high" if fragment else "low",
            validity_status="current", first_observed_at=now,
            expires_at=now + timedelta(days=evidence_days),
            artifact_keys=[digest], coverage_keys=[digest], score_eligible=bool(fragment),
            discovery_only=not bool(fragment),
            metadata={"feature": feature,
                      "role": "fit_demand_timing_access" if fragment else "research_coverage",
                      "navigation_links": list(links)},
        )
        summary = ScannerExecutionSummary(
            scanner_id=SCANNER_ID, scanner_version=version, status="success",
            company_key=company["company_key"], request_count=1, byte_count=size,
            started_at=now, finished_at=now,
        )
        await self.db(lambda store: store.complete_scanner_execution(
            execution, summary, artifacts=[artifact], coverage_manifests=[coverage],
            observations=[observation], next_due_at=now + timedelta(days=REFRESH_DAYS),
        ))
        await self.company_success(company)

    async def page(self, company, url, feature, planner):
        base = "https://" + company["company_key"] + "/"
        await self.check_target(url, base)
        params = page_parameters(url, feature, self.options)
        receipt = await self.request(company, params, feature)
        if receipt is None:
            def navigation(store):
                row = store.connection.execute(
                    "SELECT metadata_json FROM atomic_observations WHERE company_key=? "
                    "AND scanner_id=? AND predicate='microlink.page' AND source_url=? "
                    "ORDER BY id DESC LIMIT 1", (company["company_key"], SCANNER_ID, url),
                ).fetchone()
                return json.loads(row[0]).get("navigation_links", []) if row else []
            for link in await self.db(navigation):
                planner._add(link, url)
            return
        try:
            data = receipt[2]
            status = data.get("statusCode", 200)
            if not isinstance(status, int) or not 200 <= status < 400:
                raise ValueError("Microlink target was not a public successful page")
            if "function" in params:
                function = data.get("function", {})
                if function.get("isFulfilled") is not True:
                    raise ValueError("Microlink browser interaction failed")
                data = function.get("value", {})
            final = data.get("url") or url
            safe = _own_url(final, base, document=feature == "documents")
            if not safe:
                raise UnsafeTargetError("Microlink final URL crossed company identity")
            await self.check_target(safe, base)
            html = data.get("html")
            if not isinstance(html, str) or not html.strip() or len(html.encode()) > MAX_BYTES:
                raise ValueError("Microlink rendered HTML was empty or oversized")
            from .inspect import _extract_page

            now = receipt[5]
            encoded = html.encode()
            page, signals, links, technologies, contacts, ids, web = await _parse(
                len(encoded), lambda: _extract_page(
                    safe, 200, encoded, research=planner, observed_at=now))
            page_title = (page.title or "").casefold()
            page_text = (page.text_excerpt or "").casefold()
            if (("cloudflare" in page_title and "attention required" in page_title)
                    or ("why have i been blocked?" in page_text
                        and ("you are unable to access" in page_text
                             or "cloudflare ray id" in page_text))):
                raise ValueError("Microlink returned an access-block page")
            if page.canonical_url and company_host(page.canonical_url) != company["company_key"]:
                raise UnsafeTargetError("Microlink canonical identity belongs to another company")
            # Contacts own their new source clock; the saved homepage remains unchanged.
            for item in [*contacts.emails, *contacts.phones, *contacts.people]:
                item.observed_at = now
            if feature == "technologies":
                insights = receipt[2].get("insights", {})
                for item in insights.get("technologies", []):
                    if (isinstance(item, dict) and isinstance(item.get("name"), str)
                            and isinstance(item.get("confidence"), (int, float))
                            and item["confidence"] >= 80):
                        technologies.append(item["name"][:100])
                from .inspect import CREATIVE_TECHNOLOGIES, GROWTH_TECHNOLOGIES

                aliases = {"Facebook Pixel": "Meta Pixel",
                           "Google Analytics": "Google Ads/Analytics",
                           "Google Tag Manager": "Google Ads/Analytics"}
                technologies = [aliases.get(name, name) for name in technologies]
                web.sales.growth_technologies = [
                    name for name in technologies if name in GROWTH_TECHNOLOGIES]
                web.sales.creative_technologies = [
                    name for name in technologies if name in CREATIVE_TECHNOLOGIES]
            if feature == "media":
                # Resolved locators retain company-page attribution, not an invented cadence.
                web.source_urls.extend(
                    str(receipt[2][kind]["url"]) for kind in ("video", "audio")
                    if isinstance(receipt[2].get(kind), dict)
                    and isinstance(receipt[2][kind].get("url"), str)
                    and is_allowed(receipt[2][kind]["url"])
                )
            fragment = SiteObservation(
                hostname=company["hostname"], canonical_host=company["company_key"],
                observed_at=now, live=True, fetch_outcome="live", fetch_provider="microlink",
                final_url=safe, status_code=200, pages=[page], title=page.title,
                description=page.description, text_excerpt=page.text_excerpt,
                content_hash=page.content_hash, language=page.language, contacts=contacts,
                emails=[item.address for item in contacts.emails if item.outreach_eligible],
                technologies=list(dict.fromkeys(technologies)), public_ids=ids, web=web,
                signals=signals, evidence_urls=[safe],
            )
            await self.publish(company, receipt, feature, fragment=fragment, url=safe,
                               requested_url=url,
                               links=[link for link in links if _own_url(link, base)
                                      or _own_url(link, base, document=True)])
        except BaseException as exc:
            reason = ("uncertain:" if isinstance(exc, asyncio.CancelledError)
                      else "extraction:") + type(exc).__name__
            await self.db(lambda store: self.fail_pending(store, receipt[0], reason, "failed"))
            raise

    async def search(self, company, feature):
        host = company["company_key"]
        name = str(company.get("name") or host).replace('"', " ")[:160]
        query = {
            "web_search": f'site:{host} (team OR marketing OR careers OR creative OR procurement)',
            "news_search": f'"{name}" (launch OR campaign OR marketing OR expansion)',
            "business_listings": f"{name} {host}" if name != host else host,
        }[feature]
        if self.contact_refresh and feature == "web_search":
            query = (
                f"site:{host} (leadership OR team OR marketing OR "
                '"chief marketing officer" OR "head of growth")'
            )
        surface = {"web_search": "search", "news_search": "news",
                   "business_listings": "places"}[feature]
        target = f"https://microlink.google/{quote_plus(query)}/10/us?type={surface}"
        if surface == "news":
            target += "&period=month"
        params = {
            "url": target, "meta": "false", "ttl": "1d", "staleTtl": "0",
        }
        receipt = await self.request(company, params, feature)
        if receipt is None:
            def prior(store):
                row = store.connection.execute(
                    "SELECT value_json FROM atomic_observations WHERE company_key=? "
                    "AND scanner_id=? AND scanner_version=? "
                    "AND predicate='microlink.search_results' "
                    "AND expires_at>? ORDER BY id DESC LIMIT 1",
                    (host, SCANNER_ID, "1." + _hash(params)[:24], datetime.now(UTC).isoformat()),
                ).fetchone()
                return json.loads(row[0]) if row else []
            results = await self.db(prior)
        else:
            raw = receipt[2].get("results")
            if not isinstance(raw, list):
                await self.db(lambda store: store.fail_scanner_execution(
                    receipt[0], error="invalid:search_results", status="failed"))
                raise ValueError("Microlink search result shape was invalid")
            results = [item for item in raw if isinstance(item, dict)
                       and isinstance(item.get("url"), str) and is_allowed(item["url"])]
            await self.publish(company, receipt, feature, results=results, url=target)
        return [item["url"] for item in results
                if company_host(item["url"]) == host]

    async def company(self, company):
        if await self.company_held(company):
            self.failures += 1
            return
        snapshot = await self.db(lambda store: store.latest_snapshots(
            [company["hostname"]]).get(company["hostname"]))
        if not snapshot:
            return
        observation = snapshot["observation"]
        from .naming import concise_company_name

        company = {**company, "name": concise_company_name(
            label_hint="", title=observation.title, hostname=company["hostname"],
        )}
        base = observation.final_url or "https://" + company["company_key"] + "/"
        if company_host(base) != company["company_key"]:
            return
        planner = SiteResearchPlanner(base)
        links = [*observation.contact_urls, *observation.web.career_urls,
                 *observation.evidence_urls]
        search_links = []
        for feature in ("web_search", "news_search", "business_listings"):
            if getattr(self.options, feature):
                try:
                    search_links.extend(await self.search(company, feature))
                except MicrolinkLimitReached:
                    raise
                except CompanyResearchDeferred:
                    self.failures += 1
                    return
                except Exception as exc:
                    await self.company_failure(company, exc)
                    if await self.company_held(company):
                        return
        if not any((self.options.contacts, self.options.company_pages, self.options.documents,
                    self.options.technologies, self.options.media, search_links)):
            return
        # Free navigation discovery; it never marks saved observations fresh.
        await self.check_target(base, base)
        try:
            async with asyncio.timeout(PUBLIC_FETCH_SECONDS):
                response, body = await _bounded_get(
                    self.client, base, max_bytes=MAX_BYTES,
                    redirect_validator=lambda current, target: self.check_target(target, base))
            if response.is_success and body:
                from .html_document import parse_html

                def navigation():
                    soup = parse_html(body.decode("utf-8", errors="replace"))
                    planner.observe(base, soup)
                    return [urljoin(base, str(a["href"])) for a in soup.select("a[href]")]

                links.extend(await _parse(len(body), navigation))
        except (httpx.HTTPError, TimeoutError):
            pass  # Selected paid page retrieval still has its own guarded path.
        queue = [(url, "search_result") for url in dict.fromkeys(search_links)][:3]
        for link in links:
            purpose = purpose_for(link)
            if self.options.contacts and purpose in {"contact", "company"}:
                queue.append((link, "contacts"))
            if self.options.company_pages and purpose in {
                "hiring", "procurement", "proof", "production", "release", "content", "catalog",
            }:
                queue.append((link, "company_pages"))
            if self.options.documents and urlsplit(link).path.lower().endswith(DOCUMENT_SUFFIXES):
                queue.append((link, "documents"))
        # Render the root when no linked surface exists; dynamic roots can reveal the frontier.
        if self.options.contacts and not any(f == "contacts" for _, f in queue):
            queue.append((base, "contacts"))
        if self.options.company_pages and not any(f == "company_pages" for _, f in queue):
            queue.append((base, "company_pages"))
        if self.options.technologies:
            queue.insert(0, (base, "technologies"))
        if self.options.media:
            queue.insert(0, (base, "media"))
        # Give every selected page capability a first turn before deepening any one.
        first = []
        for feature in ("contacts", "company_pages", "documents", "technologies", "media",
                        "search_result"):
            item = next((item for item in queue if item[1] == feature), None)
            if item:
                first.append(item)
                queue.remove(item)
        queue = first + queue
        seen = set()
        attempts = 0
        while queue and attempts < PAGE_LIMIT:
            link, feature = queue.pop(0)
            url = _own_url(link, base, document=feature == "documents")
            if not url:
                continue
            identity = (url_key(url), feature if feature in {"technologies", "media"} else "page")
            if identity in seen:
                continue
            seen.add(identity)
            attempts += 1
            try:
                await self.page(company, url, feature, planner)
            except MicrolinkLimitReached:
                raise
            except CompanyResearchDeferred:
                self.failures += 1
                return
            except Exception as exc:
                await self.company_failure(company, exc)
                if await self.company_held(company):
                    return
            for item in planner.links.values():
                if self.options.contacts and item.purpose in {"contact", "company"}:
                    queue.append((item.url, "contacts"))
                elif self.options.company_pages:
                    queue.append((item.url, "company_pages"))


async def run_hydration(database, run_id, options, api_key, cohort, *, checkpoint, progress,
                        client=None, contact_refresh=False, force_contacts=False,
                        concurrency=COMPANY_CONCURRENCY):
    """One owner, bounded attempts, shared provider controls; completed receipts survive stop."""
    if not options.enabled:
        return {"completed": 0, "failed": 0, "requests": 0}
    if not api_key:
        raise ValueError("Configure Microlink in Settings before enabling hydration")
    if client is None:
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        async with httpx.AsyncClient(timeout=45, limits=limits) as owned:
            return await run_hydration(database, run_id, options, api_key, cohort,
                                       checkpoint=checkpoint, progress=progress, client=owned,
                                       contact_refresh=contact_refresh,
                                       force_contacts=force_contacts, concurrency=concurrency)
    result = {"completed": 0, "failed": 0, "requests": 0}
    db_lock, progress_lock, stopped = asyncio.Lock(), asyncio.Lock(), asyncio.Event()
    hydrator = Hydrator(database, run_id, options, api_key, client, checkpoint, progress,
                        db_lock=db_lock, stopped=stopped, contact_refresh=contact_refresh,
                        force_contacts=force_contacts)
    completed, failed = set(), set()
    pending = iter(cohort[:options.company_limit])

    async def worker():
        while not stopped.is_set():
            await checkpoint()
            if stopped.is_set():
                return
            company = next(pending, None)
            if company is None:
                return
            # Each company keeps its own parser frontier, robots cache and failure
            # counter; a failing peer cannot mark healthy work as failed.
            owner = Hydrator(database, run_id, options, api_key, client, checkpoint, progress,
                             db_lock=db_lock, stopped=stopped, contact_refresh=contact_refresh,
                             force_contacts=force_contacts)
            try:
                await owner.company(company)
                (failed if owner.failures else completed).add(
                    company["candidate_id"])
            except MicrolinkLimitReached as exc:
                stopped.set()
                result["limit_reached"] = True
                result.setdefault("stop_reason", str(exc))
                return
            except Exception:
                failed.add(company["candidate_id"])
            async with progress_lock:
                result["requests"] = await hydrator.db(lambda store: store.connection.execute(
                    "SELECT COUNT(*) FROM scanner_executions WHERE run_id=? AND scanner_id=?",
                    (run_id, SCANNER_ID),
                ).fetchone()[0])
                result.update(completed=len(completed), failed=len(failed))
                progress(dict(result))

    with rate_limit_scope(database):
        # A fixed pool bounds retained responses/tasks even for a 10,000-company
        # cohort. Quota/ceiling stops drain admitted work; user Stop cancels and
        # drains every worker through the existing structured ownership helper.
        try:
            await gather_owned(*(worker() for _ in range(min(
                concurrency, options.company_limit, len(cohort)))))
        except asyncio.CancelledError:
            def settle_pending(store):
                rows = store.connection.execute(
                    "SELECT id FROM scanner_executions WHERE run_id=? AND scanner_id=? "
                    "AND status='running'", (run_id, SCANNER_ID),
                ).fetchall()
                for row in rows:
                    hydrator.fail_pending(store, row[0], "uncertain:CancelledError")

            # A worker can be cancelled while its reservation commits or before
            # its response is published. All workers have drained at this point.
            await hydrator.db(settle_pending)
            raise
        result["requests"] = await hydrator.db(lambda store: store.connection.execute(
            "SELECT COUNT(*) FROM scanner_executions WHERE run_id=? AND scanner_id=?",
            (run_id, SCANNER_ID),
        ).fetchone()[0])
        # Resume must retain failed/uncertain receipts rather than turn skipped attempts
        # into a successful company completion just because no second call was made.
        failed.update(await hydrator.db(lambda store: [row[0] for row in store.connection.execute(
            "SELECT DISTINCT candidate_id FROM scanner_executions WHERE run_id=? AND scanner_id=? "
            "AND status NOT IN ('success','success_empty','not_applicable')",
            (run_id, SCANNER_ID),
        )]))
        result.update(completed=len(completed - failed), failed=len(failed))
    return result
