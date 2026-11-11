import { captureSalesLocation, restoreSalesLocation, restoreSalesScroll, contactLookupActions, runContactLookup, openManualProspect, loadSalesWorkbench, openSalesCompany, browseCompanyKey, compilationErrorText, configureSalesWorkbench, loadSalesMissions, needsSalesMission, setSalesMode, salesMode, searchSalesWorkbench, salesSearch, deactivateSalesWorkbench, invalidateSalesWorkbench, updateFirstLaunchKey, VM_SETUP_GUIDE } from "./sales-engine.js?v=20261110-contacts-table";
import { installScanPaneDivider } from "./company-website.js?v=11";
const state = {
  dashboard: null,
  accountDashboard: null,
  activeView: "sales",
  initializedViews: new Set(),
  pageSearch: "",
  accounts: [],
  accountTotal: 0,
  accountOffset: 0,
  accountRequestId: 0,
  activeAccount: null,
  accountImportText: "",
  prospects: [],
  prospectTotal: 0,
  prospectOffset: 0,
  prospectRequestId: 0,
  dashboardRequestId: 0,
  overviewSort: "priority",
  targetStatus: null,
  targetShortfall: null,
  tags: [],
  sources: [],
  sourceSort: { key: null, direction: "asc" },
  discoveryItems: [],
  acceptedSources: [],
  pendingSourceDismissals: new Set(),
  runs: [],
  research: null,
  intelligence: null,
  scanStatus: null,
  scanPollTimer: null,
  scanPollInFlight: false,
  smartScan: null,
  smartScanStarting: false,
  smartScanLoading: false,
  smartScanControlPending: false,
  smartScanDueHydrationEnabled: true,
  smartScanAdsEnabled: false,
  smartScanAdsCompanyLimit: 100,
  smartScanAdsMaxUSD: 5,
  smartScanPeopleEnabled: false,
  smartScanPeopleCompanyLimit: 100,
  smartScanSourceAIEnabled: false,
  smartScanMicrolinkEnabled: false,
  smartScanMicrolinkHydration: {company_limit:100, request_limit:500},
  smartScanAIEnabled: false,
  smartScanAISiteLimit: 100,
  smartScanSEOEnabled: false,
  smartScanSEOSiteLimit: 100,
  smartScanFailureSkipEnabled: false,
  smartScanFailureRate: 75,
  smartScanControlsInitialized: false,
  smartScanPreset: "basic",
  smartScanPollTimer: null,
  smartScanPollInFlight: false,
  smartScanElapsedTimer: null,
  smartScanTerminalTab: "activity",
  smartScanTerminalWrap: true,
  smartScanErrors: [],
  smartScanErrorOffset: 0,
  smartScanErrorTotal: 0,
  smartScanErrorsHasMore: false,
  smartScanErrorRunId: null,
  smartScanLiveErrorSeen: new Set(),
  smartScanErrorsLoading: false,
  smartScanActionRunId: null,
  smartScanActionSeen: 0,
  smartScanActionQueue: [],
  smartScanCurrentAction: null,
  smartScanActionTimer: null,
  megaStarting: false,
  webScanEstimate: null,
  webScanStarting: false,
  scoutPollTimer: null,
  scoutPollInFlight: false,
  scoutActivityTimer: null,
  scoutHeartbeatAt: null,
  adDiscoveryOverview: null,
  adDiscoveryLinks: [],
  adDiscoveryLinkTotal: 0,
  adDiscoveryEstimate: null,
  adDiscoveryRun: null,
  adDiscoveryApprovedCap: null,
  adDiscoveryPollTimer: null,
  adDiscoveryPollInFlight: false,
  adDiscoveryStarting: false,
  adDiscoveryLinkFilter: "all",
  discoveryTab: "web_ct",
  discoveryMethods: null,
  discoveryEstimates: {},
  discoveryRun: null,
  discoveryRuns: [],
  discoveryPollTimer: null,
  discoveryPollInFlight: false,
  discoveryStarting: false,
  webScanStatus: null,
  scanOverview: null,
  webScanJobId: null,
  sourceScoutStarting: false,
  scanReadiness: null,
  peopleHydration: null,
  peopleHydrationQuote: null,
  peopleHydrationStarting: false,
  peopleHydrationPollTimer: null,
  peopleHydrationPollInFlight: false,
  filters: {
    search: "", lane: "buyer", scope: "direct", min_score: "", min_principal_signals: "", review_ready: "", archetype: "", sort: "priority",
  },
  accountFilters: {
    cohort: "prototype-v1", search: "", relationship_type: "", segment: "",
    lead_source: "", qualification_state: "", lifecycle: "", min_score: "",
  },
};

const TARGET_OVERVIEW_CACHE_KEY = "beings-growth:target-overview:v2";
const SOURCES_VIEW_CACHE_KEY = "beings-growth:sources-view:v1";
let sourceReviewGeneration = 0;
const REFRESH_STATE_KEY = "beings-growth:refresh-state:v1";

const RESULT_PAGE_SIZE = 200;
const SMART_SCAN_AI_ESTIMATE_DEFAULTS = Object.freeze({
  maximumSites: 100,
  sourceToolCalls: 10,
  sourceCostPerToolCall: .02,
  candidateCostPerSite: .011,
  candidateWebSites: 8,
  candidateWebCostPerSite: .02,
});

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const ACCOUNT_SEGMENTS = ["consumer_brand", "consumer_app", "digital_product", "gaming", "subscription_business", "saas_ai_adjacency", "agency_buyer", "agency_referral_partner", "other"];
const ACCOUNT_LIFECYCLES = ["research", "qualified", "outreach", "discovery", "proposal", "won", "lost", "paused"];
const QUALIFICATION_STATES = ["needs_research", "pursue", "disqualified"];
const TRUTH_STATES = ["unknown", "met", "not_met"];
const PRINCIPAL_SIGNALS = ["distribution_investment", "recurring_creative_demand", "production_capacity_gap"];
const QUALIFICATION_GATES = ["recurring_work_3_months", "three_immediate_asks", "economic_buyer", "ai_permissible", "one_specialist_lane", "annual_capacity_60k"];
const DIMENSION_LIMITS = { distribution_investment: 25, recurring_creative_demand: 20, production_capacity_gap: 25, ai_brand_readiness: 15, timing_access: 15 };
const DEFAULT_META_DISCOVERY_QUERIES = ["performance creative agency", "video production agency"];
const ACTIVE_RUN_STATES = new Set(["queued", "starting", "running", "retrying", "cancelling"]);
const SOURCE_SORT_COLUMNS = [
  { key: "apex", label: "Source", type: "text" },
  { key: "opportunity_score", label: "Rank", type: "number" },
  { key: "inspected_count", label: "Sample", type: "number" },
  { key: "live_rate", label: "Live yield", type: "number" },
  { key: "high_priority_yield", label: "Priority yield", type: "number" },
  { key: "placement_ready_yield", label: "Placement yield", type: "number" },
  { key: "contactable_yield", label: "Contact yield", type: "number" },
  { key: "scan_failure_rate", label: "Avg. failure", type: "number" },
  { key: "backlog", label: "Backlog", type: "number" },
  { key: "enabled", label: "Web & CT status", type: "boolean" },
];
const SCAN_SECTION_LINKS = [
  ["smart-scan-due-hydration", "Company refresh"],
  ["smart-scan-skip-failures", "Failure filtering"],
];
const DISCOVERY_TAB_DEFINITIONS = [
  { id: "web_ct", label: "Web & CT", source_kinds: [], kind: "web" },
  { id: "company_intelligence", label: "Company intelligence", source_kinds: [], kind: "method" },
  { id: "tenant_footprints", label: "Tenant footprints", source_kinds: ["tenant_apex"], kind: "method" },
  { id: "directories", label: "Directories", source_kinds: ["agency_directory", "brand_directory"], kind: "method" },
  { id: "portfolios", label: "Portfolios", source_kinds: ["brand_group_portfolio"], kind: "method" },
  { id: "customer_lists", label: "Customer lists", source_kinds: ["platform_customer_list"], kind: "method" },
  { id: "searches", label: "Searches", source_kinds: ["repeatable_search"], kind: "method" },
  { id: "public_graph", label: "Public graph", source_kinds: [], kind: "method" },
  { id: "paid_ads", label: "Paid ads", source_kinds: [], kind: "paid" },
  { id: "find_sources", label: "Find sources", source_kinds: [], kind: "scout" },
];

function enhanceMultiChecklists(root = document) {
  $$('fieldset:not([data-checklist-enhanced])', root).forEach(fieldset => {
    const items = $$('input[type="checkbox"]:not([hidden])', fieldset)
      .filter(input => input.closest("fieldset") === fieldset);
    if (items.length < 2) return;
    fieldset.dataset.checklistEnhanced = "true";
    fieldset.classList.add("multi-checklist");
    const action = document.createElement("button");
    action.type = "button";
    action.className = "checklist-select-all";
    const enabledItems = () => items.filter(input => !input.disabled);
    const sync = () => {
      const available = enabledItems();
      const selected = available.filter(input => input.checked).length;
      const allSelected = available.length > 0 && selected === available.length;
      action.textContent = allSelected ? "Clear all" : "Select all";
      action.disabled = available.length === 0;
      action.setAttribute("aria-label", `${action.textContent}; ${selected} of ${available.length} available selected`);
    };
    action.addEventListener("click", () => {
      const available = enabledItems();
      const shouldSelect = available.some(input => !input.checked);
      available.forEach(input => {
        if (input.checked === shouldSelect) return;
        input.checked = shouldSelect;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
      sync();
    });
    items.forEach(input => input.addEventListener("change", sync));
    const legend = $("legend", fieldset);
    if (legend) {
      const separator = document.createElement("span");
      separator.className = "checklist-select-all-separator";
      separator.textContent = " · ";
      legend.append(separator, action);
    } else fieldset.prepend(action);
    sync();
  });
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

// Native disabling blocks pointer and keyboard activation; snapshots preserve setup locks.
const scanControlLocks = new WeakMap();
function setScanControlsLocked(root, locked) {
  if (!root) return;
  if (locked) {
    if (scanControlLocks.has(root)) return;
    const controls = [...root.querySelectorAll("button, input, select, textarea")];
    scanControlLocks.set(root, controls.map(control => [control, control.disabled]));
    root.dataset.scanBusy = "true";
    root.setAttribute("aria-busy", "true");
    controls.forEach(control => { control.disabled = true; });
  } else {
    const controls = scanControlLocks.get(root);
    if (!controls) return;
    controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    scanControlLocks.delete(root);
    delete root.dataset.scanBusy;
    root.setAttribute("aria-busy", "false");
  }
}

function scanActionBlocked(control) {
  return !control || control.matches(":disabled") || Boolean(control.closest('[data-scan-busy="true"]'));
}

function smartScanLaunchBlocked(scan = state.smartScan) {
  return !scan || state.smartScanLoading || state.smartScanGoalPending
    || Boolean(scanLaunchConflict("smart", scan));
}

function scanLaunchConflict(mode, scan = state.smartScan) {
  if (state.importScanStarting || state.microlinkScanStarting
      || state.smartScanStarting || state.smartScanControlPending) return "Scan action in progress.";
  if (state.microlinkScanActive) return "A Microlink scan is active.";
  if (mode === "import" && scan?.cleaning_up) return "";
  const live = state.scanOverview;
  if (scan?.cleaning_up) return "Smart Scan is finishing cleanup.";
  if (scan?.status === "paused") return "Smart Scan is paused. Resume or stop it first.";
  const summary = live?.smart;
  const staleSummary = scan?.id && scan.id === summary?.id
    && Date.parse(scan.updated_at) >= Date.parse(summary.updated_at);
  if ((scan && smartScanBusy(scan)) || (!staleSummary && summary && smartScanActive(summary))) {
    return "Smart Scan is active.";
  }
  if ((scan?.external_source_runs ?? live?.methods)?.length) return "A public-source scan is active.";
  if (state.webScanStarting || ACTIVE_RUN_STATES.has(state.scanStatus)) return "A company scan is active.";
  if (mode !== "import" && ACTIVE_RUN_STATES.has(live?.paid?.status)) return "A paid-ad scan is active.";
  if (mode === "smart" && (ACTIVE_RUN_STATES.has(live?.scout?.status)
      || ACTIVE_RUN_STATES.has(live?.people?.status))) return "Smart Scan is unavailable while other research runs.";
  return "";
}

function loadingIndicator(label = "Loading…", {iconOnly = false} = {}) {
  const spinner = '<span class="loading-spinner" aria-hidden="true"></span>';
  if (iconOnly) {
    return `<span class="loading-indicator" role="status" aria-label="${escapeHtml(label)}">${spinner}</span>`;
  }
  return `<span class="loading-indicator" role="status">${spinner}<span>${escapeHtml(label)}</span></span>`;
}

function clearLoadingIndicators(selector, message = "Unavailable") {
  $$(selector).forEach(root => {
    root.querySelectorAll(".loading-indicator").forEach(indicator => {
      indicator.replaceWith(document.createTextNode(message));
    });
  });
}

function scanTerminology(value = "") {
  return String(value)
    .replace(/\bDiscovery\b/g, "Scan")
    .replace(/\bdiscovery\b/g, "scan")
    .replace(/\bRunning\b/g, "Scanning")
    .replace(/\brunning\b/g, "scanning")
    .replace(/\bRuns\b/g, "Scans")
    .replace(/\bruns\b/g, "scans")
    .replace(/\bRun\b/g, "Scan")
    .replace(/\brun\b/g, "scan");
}

function normalizeDisplayedTerminology(root = document.body) {
  if (!root) return;
  const elements = root.nodeType === Node.ELEMENT_NODE ? [root, ...root.querySelectorAll("*")] : [];
  elements.forEach(element => {
    if (element.closest("#view-sales, #account-workspace")) return;
    ["aria-label", "title", "placeholder", "alt"].forEach(attribute => {
      if (!element.hasAttribute(attribute)) return;
      const current = element.getAttribute(attribute);
      const normalized = scanTerminology(current);
      if (normalized !== current) element.setAttribute(attribute, normalized);
    });
  });
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let textNode;
  while ((textNode = walker.nextNode())) {
    if (textNode.parentElement?.closest("#view-sales, #account-workspace, [data-verbatim]")) continue;
    const normalized = scanTerminology(textNode.nodeValue);
    if (normalized !== textNode.nodeValue) textNode.nodeValue = normalized;
  }
}

function safeUrl(value = "") {
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "#";
  } catch { return "#"; }
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

function relativeTime(value) {
  if (!value) return "Never";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const ranges = [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.345, "week"], [12, "month"]];
  let duration = seconds;
  for (const [amount, unit] of ranges) {
    if (Math.abs(duration) < amount) return formatter.format(Math.round(duration), unit);
    duration /= amount;
  }
  return formatter.format(Math.round(duration), "year");
}

function absoluteDate(value) {
  if (!value) return "—";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) {
    return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(`${value}T00:00:00Z`));
  }
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

function localDateTimeValue(value) {
  if (!value) return "";
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function prospectParams({ limit = RESULT_PAGE_SIZE, offset = state.prospectOffset } = {}) {
  const params = new URLSearchParams();
  if (limit) params.set("limit", String(limit));
  if (offset) params.set("offset", String(offset));
  Object.entries(state.filters).forEach(([key, value]) => {
    if (value !== "" && value !== 0) params.set(key, value);
  });
  return params;
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const area = document.createElement("textarea");
  area.value = value; area.style.position = "fixed"; area.style.opacity = "0";
  document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
}

async function api(path, options = {}) {
  const { headers = {}, invalidateWorkbench = true, ...requestOptions } = options;
  const cache = requestOptions.cache ?? (requestOptions.method ? undefined : "no-store");
  const response = await fetch(path, { ...requestOptions, cache, headers: { "Content-Type": "application/json", ...headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => item.msg || JSON.stringify(item)).join("; ")
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  if (invalidateWorkbench && requestOptions.method && !["GET", "HEAD"].includes(requestOptions.method.toUpperCase())) invalidateSalesWorkbench();
  if (response.status === 204) return null;
  return response.json();
}

const scanEstimateRefresh = new WeakMap();

function bindAutomaticScanEstimate(form, estimate, clearQuote) {
  const status = document.createElement("p");
  status.className = "form-note scan-auto-estimate-status";
  status.setAttribute("role", "status");
  status.hidden = true;
  form.append(status);
  let timer;
  let controller;
  let lastDraft;
  const draftKey = () => JSON.stringify([selectedScanMission(), [...new FormData(form)]]);
  function refresh(event) {
    if (scanControlLocks.has(form)) return;
    if (event && draftKey() === lastDraft) return;
    clearTimeout(timer);
    controller?.abort();
    clearQuote();
    lastDraft = draftKey();
    status.hidden = true;
    if (!form.getClientRects().length || !form.checkValidity()) return;
    const request = new AbortController();
    controller = request;
    status.textContent = "Checking cost…";
    status.hidden = false;
    timer = setTimeout(async () => {
      const isCurrent = () => !request.signal.aborted && form.isConnected
        && form.getClientRects().length > 0 && !scanControlLocks.has(form);
      if (!isCurrent()) {
        lastDraft = null;
        status.hidden = true;
        return;
      }
      try {
        await estimate({currentTarget:form, preventDefault() {}, signal:request.signal, isCurrent});
        if (isCurrent()) status.hidden = true;
      } catch (error) {
        if (isCurrent()) {
          lastDraft = null;
          status.textContent = error.message;
        }
      } finally {
        if (controller === request && !isCurrent()) {
          lastDraft = null;
          status.hidden = true;
        }
      }
    }, 350);
  }
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  form.addEventListener("submit", event => { event.preventDefault(); refresh(); });
  scanEstimateRefresh.set(form, refresh);
  return refresh;
}

function refreshSelectedScanEstimate() {
  const dialog = $("#smart-scan-options-dialog");
  if (!dialog?.open) return;
  const tab = state.discoveryTab;
  const form = tab === "web_ct" ? $("#web-scan-form", dialog)
    : tab === "paid_ads" ? $("#paid-discovery-form", dialog)
    : $$("[data-discovery-method-form]", dialog).find(node => node.dataset.discoveryMethodForm === tab);
  if (!form?.closest("[hidden]")) scanEstimateRefresh.get(form)?.();
}

async function requestScanEstimate(event, endpoint, config) {
  const draft = JSON.stringify(config());
  const mission = selectedScanMission();
  const data = await api(endpoint, {
    method: "POST", body: draft, signal: event.signal, invalidateWorkbench:false,
  });
  if (event.isCurrent?.() === false || mission !== selectedScanMission() || draft !== JSON.stringify(config())) return null;
  return data;
}

function scanToastTone(status) {
  if (["failed", "partial", "interrupted", "error"].includes(status)) return "error";
  return ["complete", "completed"].includes(status) ? "success" : "info";
}

function showToast(message, tone = "success") {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.tone = tone;
  toast.textContent = roundDollarText(scanTerminology(message));
  $("#toasts").append(toast);
  setTimeout(() => toast.remove(), tone === "error" ? 6500 : 4000);
}

function setPageSearchStatus(value = "") {
  const status = $("#page-search-status");
  if (status && status.textContent !== value) status.textContent = value;
}

function updatePageSearchContext() {
  const isCompanies = state.activeView === "sales";
  const form = $("#page-search-form");
  form.hidden = !isCompanies && state.activeView !== "prospects";
  if (form.hidden) return;

  if (isCompanies) state.pageSearch = salesSearch();
  const input = $("#page-search");
  const label = isCompanies ? "Search companies" : "Search prospects";
  input.placeholder = label;
  form.querySelector(".sr-only").textContent = label;
  if (input.value !== state.pageSearch) input.value = state.pageSearch;
}

function localPageSearchUnits(view = state.activeView) {
  const selectors = {
    sources: ["#sources-suggestion-body tr", "#active-discovery-source-body tr", "#source-groups tbody tr"],
    intelligence: ["[data-discovery-tab]"],
    settings: [".settings-card", ".credential-row"],
    spend: [".spend-service-row", ".spend-provider-row"],
    research: ["#mission-list > *", "#probe-list > *"],
    runs: ["#run-list > *"],
  }[view] || [];
  return selectors.flatMap(selector => $$(selector));
}

function applyLocalPageSearch() {
  if (state.activeView === "sales") { setPageSearchStatus(""); return; }
  const query = state.pageSearch.trim().toLowerCase();
  const units = localPageSearchUnits();
  if (!units.length) {
    setPageSearchStatus(query ? "No searchable results" : "");
    return;
  }
  let matches = 0;
  units.forEach(unit => {
    const searchText = `${unit.dataset.searchText || ""} ${unit.textContent || ""}`.toLowerCase();
    const matched = !query || searchText.includes(query);
    unit.hidden = !matched;
    if (matched) matches += 1;
  });
  if (state.activeView === "sources") {
    $$("#source-groups .source-group").forEach(group => {
      group.hidden = query && ![...group.querySelectorAll("tbody tr")].some(row => !row.hidden);
    });
  }
  setPageSearchStatus(query ? `${formatNumber(matches)} match${matches === 1 ? "" : "es"}` : "");
}

async function executePageSearch() {
  const query = state.pageSearch.trim();
  if (state.activeView === "sales") { await searchSalesWorkbench(query); return; }
  if (state.activeView === "prospects") { outboundOffset = 0; await loadOutboundProspects(); return; }
  if (state.activeView === "overview") {
    state.filters.search = query;
    state.prospectOffset = 0;
    if (state.activeView === "overview" && query) {
      state.filters.lane = "all";
      state.filters.scope = "all";
      await setView("prospects", { preserveSearch: true });
    } else if (state.activeView === "prospects") {
      await loadProspects();
    } else {
      setPageSearchStatus("");
    }
    return;
  }
  if (state.activeView === "accounts") {
    state.accountFilters.search = query;
    state.accountOffset = 0;
    await loadAccounts();
    return;
  }
  applyLocalPageSearch();
}

function companyName(item) {
  const name = item.company || item.company_name || item.canonical_host || item.hostname;
  return name === "Home" ? (item.canonical_host || item.hostname) : name;
}

function humanize(value = "") {
  return scanTerminology(String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase()));
}

function percentage(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number <= 1 ? (number * 100).toFixed(1) : number.toFixed(1)}%`;
}

const usdFormatter = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD"});
const subcentUsdFormatter = new Intl.NumberFormat("en-US", {
  style:"currency", currency:"USD", maximumFractionDigits:6,
});

function money(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const formatter = number !== 0 && Math.abs(number) < 0.01
    ? subcentUsdFormatter : usdFormatter;
  return formatter.format(number);
}

function roundDollarText(value) {
  return String(value).replace(/\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?/g,
    (_, whole, fraction) => money(whole.replaceAll(",", "") + (fraction ? `.${fraction}` : "")));
}

function maximumCostMoney(value) {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "Unknown";
  const number = Number(value);
  return money(number > 0 ? Math.ceil((number - Number.EPSILON) * 100) / 100 : number);
}

function costFrom(source, usdKeys = [], microKeys = [], fallback = null) {
  const direct = firstValue(source, usdKeys, null);
  if (direct != null && direct !== "" && Number.isFinite(Number(direct))) return Number(direct);
  const micros = firstValue(source, microKeys, null);
  if (micros != null && micros !== "" && Number.isFinite(Number(micros))) return Number(micros) / 1_000_000;
  return fallback;
}

function scanCapLabel(estimate = {}) {
  const cap = costFrom(
    estimate,
    ["maximum_charge_usd", "hard_cap_usd", "max_total_charge_usd", "request.max_total_charge_usd", "provider_cost_usd"],
    ["approved_cap_microusd", "maximum_charge_microusd", "provider_cost_microusd"],
    null,
  );
  return cap == null ? "usage caps" : cap > 0 ? maximumCostMoney(cap) : "$0.00";
}

function firstValue(source, keys, fallback = 0) {
  for (const key of keys) {
    const value = key.split(".").reduce((current, part) => current?.[part], source);
    if (value !== undefined && value !== null) return value;
  }
  return fallback;
}

function stateBadge(value, extra = "") {
  const stateValue = value || "unknown";
  return `<span class="state-badge state-${escapeHtml(stateValue)} ${escapeHtml(extra)}">${escapeHtml(humanize(stateValue))}</span>`;
}

function epistemicBadge(value, verified = false) {
  const status = value || "unknown";
  return `<span class="evidence-status evidence-${escapeHtml(status)}">${escapeHtml(humanize(status))}</span>${verified ? `<span class="verification-badge">Verified</span>` : `<span class="verification-badge unverified">Unverified</span>`}`;
}

function capacityHypothesis(item = {}) {
  const intelligence = item.target_intelligence || {};
  return intelligence.hypothesis || intelligence.hypothesis_json || item.score?.creative_capacity || {};
}

function targetIntel(item = {}) {
  const intelligence = item.target_intelligence || {};
  const hypothesis = capacityHypothesis(item);
  const principalSignals = intelligence.principal_signals || hypothesis.principal_signals || {};
  const unknownSignals = intelligence.principal_signals_unknown || Object.entries(principalSignals).filter(([, signal]) => (typeof signal === "string" ? signal : signal?.truth_state) === "unknown").map(([name]) => name);
  const criteriaGaps = Array.isArray(intelligence.criteria_gaps) ? intelligence.criteria_gaps : [];
  const rawEvidenceGaps = intelligence.evidence_gaps || hypothesis.evidence_gaps || criteriaGaps;
  const evidenceGaps = Array.isArray(rawEvidenceGaps) ? rawEvidenceGaps.filter(gap => gap?.requires !== "human_verification") : [];
  const unassessableCriteria = intelligence.unassessable_criteria || hypothesis.unassessable_criteria || criteriaGaps.filter(gap => gap?.requires === "human_verification");
  return {
    version: intelligence.version || intelligence.rubric_version || hypothesis.version || "creative-capacity-v1",
    hydration_status: intelligence.hydration_status || (Object.keys(hypothesis).length ? "hydrated" : "unhydrated"),
    lane: intelligence.lane || intelligence.relationship_lane || (item.archetype === "agency" ? "referral_partner" : "buyer"),
    scope: intelligence.scope || intelligence.target_scope || "unclassified",
    priority_score: Number(intelligence.priority_score ?? intelligence.target_score ?? hypothesis.total ?? 0),
    durable_fit_score: Number(intelligence.durable_fit_score ?? intelligence.opportunity?.durable_fit ?? item.durable_fit_score ?? 0),
    contact_now_score: Number(intelligence.contact_now_score ?? intelligence.opportunity?.contact_now ?? item.contact_now_score ?? 0),
    founder_priority_score: Number(intelligence.founder_priority_score ?? intelligence.priority_score ?? intelligence.opportunity?.founder_priority ?? item.founder_priority_score ?? 0),
    recommended_action: intelligence.recommended_action || intelligence.decision_packet?.action || item.recommended_action || "research_next",
    principal_signals_met: Number(intelligence.principal_signals_met ?? Object.values(principalSignals).filter(signal => (typeof signal === "string" ? signal : signal?.truth_state) === "met").length),
    principal_signals_unknown: Array.isArray(unknownSignals) ? unknownSignals : [],
    observed_claim_count: Number(intelligence.observed_claim_count ?? hypothesis.observations?.length ?? 0),
    inferred_claim_count: Number(intelligence.inferred_claim_count ?? hypothesis.inferences?.length ?? 0),
    evidence_gaps: evidenceGaps,
    unassessable_criteria: unassessableCriteria,
    observed_at: intelligence.observed_at || hypothesis.observed_at || item.last_seen_at || item.inspected_at || null,
    review_ready: Boolean(intelligence.review_ready ?? hypothesis.review_ready),
    leading_gap: intelligence.leading_gap || evidenceGaps[0] || unassessableCriteria[0] || null,
    derived_signals: intelligence.derived_signals || hypothesis.derived_signals || {},
    manual_gates: intelligence.manual_gates || hypothesis.manual_gates || [],
    blockers: intelligence.blockers || hypothesis.blockers || [],
    hypothesis,
  };
}

function targetAxisValue(item, name) {
  const hypothesis = targetIntel(item).hypothesis;
  return Number(hypothesis[name] ?? hypothesis.dimensions?.[name] ?? 0);
}

function targetAxes(item, { compact = false } = {}) {
  const labels = {
    distribution_investment: compact ? "Dist." : "Distribution investment",
    recurring_creative_demand: compact ? "Repeat" : "Recurring creative demand",
    production_capacity_gap: compact ? "Gap" : "Production-capacity gap",
    ai_brand_readiness: compact ? "AI" : "AI / brand readiness",
    timing_access: compact ? "Now" : "Timing / access",
  };
  return `<div class="target-axes ${compact ? "compact" : ""}">${Object.entries(DIMENSION_LIMITS).map(([name, maximum]) => {
    const value = targetAxisValue(item, name);
    return `<div class="target-axis"><span>${escapeHtml(labels[name])}</span><div><i style="width:${Math.min(100, Math.max(0, value / maximum * 100))}%"></i></div><strong>${value}<small>/${maximum}</small></strong></div>`;
  }).join("")}</div>`;
}

function signalDetail(item, name) {
  const intelligence = targetIntel(item);
  const signal = item.target_intelligence?.principal_signals?.[name] || intelligence.hypothesis.principal_signals?.[name];
  if (typeof signal === "string") return { truth_state: signal, epistemic_status: "unknown", confidence: "unknown" };
  if (signal) return { truth_state: signal.truth_state || "unknown", epistemic_status: signal.epistemic_status || "unknown", confidence: signal.confidence || "unknown" };
  if (intelligence.principal_signals_unknown.includes(name)) return { truth_state: "unknown", epistemic_status: "unknown", confidence: "unknown" };
  return { truth_state: "unknown", epistemic_status: "unknown", confidence: "unknown" };
}

function targetSignals(item, { detailed = false } = {}) {
  const abbreviations = { distribution_investment: "D", recurring_creative_demand: "R", production_capacity_gap: "G" };
  return `<div class="target-signals ${detailed ? "detailed" : ""}">${PRINCIPAL_SIGNALS.map(name => {
    const signal = signalDetail(item, name);
    const label = humanize(name);
    return detailed
      ? `<div class="target-signal signal-${escapeHtml(signal.truth_state)}"><span>${escapeHtml(label)}</span>${stateBadge(signal.truth_state)}<small>${escapeHtml(humanize(signal.epistemic_status))} · ${escapeHtml(humanize(signal.confidence))} confidence</small></div>`
      : `<span class="signal-dot signal-${escapeHtml(signal.truth_state)}" aria-label="${escapeHtml(label)}: ${escapeHtml(humanize(signal.truth_state))} (${escapeHtml(humanize(signal.epistemic_status))})">${abbreviations[name]}</span>`;
  }).join("")}<small class="signal-count">${targetIntel(item).principal_signals_met}/3 met</small></div>`;
}

function compactEvidenceCopy(value) {
  const text = String(value || "—");
  const timing = text.match(/^Public sources show (\d+) commercial triggers, (\d+) recently changed URLs, and (\d+) creative or growth openings\.$/);
  if (timing) return `${timing[1]} triggers · ${timing[2]} changes · ${timing[3]} openings`;
  const distribution = text.match(/^Public scan found (\d+) paid-distribution language signals, (\d+) marketing identifiers, and a (\d+)\/100 paid-growth stack score\.$/);
  if (distribution) return `${distribution[1]} paid signals · ${distribution[2]} marketing IDs · ${distribution[3]}/100 paid-growth stack`;
  const demand = text.match(/^Public scan found (\d+) recurring-work signals, (\d+) creative-demand signals, and (\d+) recent commercial or content items\.$/);
  if (demand) return `${demand[1]} recurring signals · ${demand[2]} demand signals · ${demand[3]} recent items`;
  const careers = text.match(/^The public careers footprint includes (\d+) creative, photo, or video openings\.$/);
  if (careers) return `${careers[1]} creative openings`;
  const assets = text.match(/^The inspected public pages contain (\d+) image assets and (\d+) visible video assets or players\.$/);
  if (assets) return `${assets[1]} images · ${assets[2]} videos`;
  if (text === "Public demand, hiring, or asset-mix evidence suggests production may be lagging distribution; it does not prove willingness to outsource.") return "Production may lag distribution";
  if (text === "The public brand and creative-technology footprint may support an AI-native production lane; rights, likeness, and model policy remain unverified.") return "AI-native production fit · rights/model policy unverified";
  return text;
}

function targetWhyNow(item) {
  const hypothesis = targetIntel(item).hypothesis;
  const value = hypothesis.why_now || hypothesis.latest_trigger || item.score?.latest_trigger || "—";
  return Array.isArray(value) ? value.filter(Boolean).map(compactEvidenceCopy).join(" · ") : compactEvidenceCopy(value);
}

function targetLeadingGap(item) {
  const intelligence = targetIntel(item);
  const gap = intelligence.leading_gap || [...intelligence.evidence_gaps, ...intelligence.unassessable_criteria, ...intelligence.blockers].find(Boolean);
  return typeof gap === "string" ? gap : gap?.label || gap?.criterion || gap?.message || "—";
}

function targetFreshness(item) {
  const intelligence = targetIntel(item);
  if (intelligence.hydration_status !== "hydrated") return humanize(intelligence.hydration_status);
  return intelligence.observed_at ? relativeTime(intelligence.observed_at) : "—";
}

function candidateAdIntelligence(item = {}) {
  return item.ad_intelligence
    || item.observation?.ad_intelligence
    || item.latest_observation?.ad_intelligence
    || item.target_intelligence?.ad_intelligence
    || item.score?.creative_capacity?.ad_intelligence
    || null;
}

function paidAdEvidencePanel(item = {}) {
  const evidence = candidateAdIntelligence(item);
  if (!evidence || typeof evidence !== "object" || !Object.keys(evidence).length) return "";
  const hasEvidence = evidence.identity_state !== "unresolved"
    || Number(evidence.total_ads || evidence.active_ads || 0) > 0
    || Boolean(evidence.meta_page_id)
    || (Array.isArray(evidence.advertiser_ids) && evidence.advertiser_ids.length)
    || (Array.isArray(evidence.source_urls) && evidence.source_urls.length);
  if (!hasEvidence) return "";
  const platforms = firstValue(evidence, ["platforms", "publisher_platforms"], []);
  const platformValues = Array.isArray(platforms) ? platforms : (typeof platforms === "string" ? [platforms] : Object.keys(platforms || {}).filter(name => platforms[name]));
  const confidence = firstValue(evidence, ["identity_confidence", "confidence", "domain_link.confidence"], null);
  const identityState = firstValue(evidence, ["identity_state", "link_state", "domain_link.state"], evidence.verified ? "verified" : "observed");
  const advertisers = firstValue(evidence, ["advertisers", "advertiser_entities"], []);
  const advertiserValues = Array.isArray(advertisers) ? [...advertisers] : [];
  if (!advertiserValues.length && evidence.meta_page_name) advertiserValues.push({ display_name: evidence.meta_page_name, external_id: evidence.meta_page_id, profile_url: evidence.meta_page_url });
  if (!advertiserValues.length && Array.isArray(evidence.advertiser_names)) advertiserValues.push(...evidence.advertiser_names.map(name => ({ display_name: name })));
  const rawSourceUrls = firstValue(evidence, ["source_urls", "evidence_urls"], []);
  const sourceUrls = Array.isArray(rawSourceUrls) ? rawSourceUrls : (rawSourceUrls ? [rawSourceUrls] : []);
  const rawCoverageNotes = evidence.coverage_notes || [];
  const coverageNotes = Array.isArray(rawCoverageNotes) ? rawCoverageNotes : [rawCoverageNotes];
  const observedAt = firstValue(evidence, ["observed_at", "last_observed_at", "updated_at"], null);
  const activeCreativeGroups = Number(firstValue(evidence, ["scoreable_active_unique_creatives", "active_unique_creatives", "active_creative_groups"], 0));
  const totalCreativeGroups = Number(firstValue(evidence, ["scoreable_unique_creatives", "unique_creatives", "creative_groups"], 0));
  const newCreativeGroups7d = Number(firstValue(evidence, ["scoreable_new_creatives_7d", "new_creatives_7d"], 0));
  const newCreativeGroups30d = Number(firstValue(evidence, ["scoreable_new_creatives_30d", "new_creatives_30d"], 0));
  const newCreativeGroups90d = Number(firstValue(evidence, ["scoreable_new_creatives_90d", "new_creatives_90d"], 0));
  const observationWindows90d = Number(firstValue(evidence, ["scoreable_observation_windows_90d", "observation_windows_90d"], 0));
  const sustainedRefresh = Boolean(evidence.recurring_established);
  return `<section class="drawer-section paid-ad-evidence">
    <div class="section-title-row"><h3>Paid-ad intelligence</h3><span class="state-badge state-${escapeHtml(identityState)}">${escapeHtml(humanize(identityState))}</span></div>
    <div class="paid-ad-summary-grid">
      <div><small>Active / total creative groups</small><strong>${formatNumber(activeCreativeGroups)} / ${formatNumber(totalCreativeGroups)}</strong></div>
      <div><small>New creative groups</small><strong>${formatNumber(newCreativeGroups7d)} / ${formatNumber(newCreativeGroups30d)}</strong><span>7d / 30d</span></div>
      <div><small>90-day refresh</small><strong>${formatNumber(newCreativeGroups90d)}</strong></div>
      <div><small>Cadence evidence</small><strong>${formatNumber(observationWindows90d)} window${observationWindows90d === 1 ? "" : "s"}</strong><span>${sustainedRefresh ? "Sustained refresh observed" : "Recurrence not established"}</span></div>
      <div><small>Platforms</small><strong>${platformValues.length ? escapeHtml(platformValues.map(humanize).join(" · ")) : "—"}</strong></div>
      <div><small>Identity</small><strong>${confidence == null ? escapeHtml(humanize(identityState)) : `${formatNumber(confidence)} confidence`}</strong></div>
    </div>
    ${(advertiserValues.length || evidence.linked_instagram_handle) ? `<div class="paid-ad-advertisers">${advertiserValues.slice(0, 4).map(advertiser => {
      const label = typeof advertiser === "string" ? advertiser : advertiser.display_name || advertiser.name || advertiser.handle || advertiser.external_id || "Advertiser";
      const url = safeUrl(typeof advertiser === "string" ? "" : advertiser.profile_url || advertiser.url || "");
      return url === "#" ? `<span>${escapeHtml(label)}</span>` : `<a href="${url}" target="_blank" rel="noopener">${escapeHtml(label)} ↗</a>`;
    }).join("")}${evidence.linked_instagram_handle ? `<span>Instagram @${escapeHtml(evidence.linked_instagram_handle.replace(/^@/, ""))}</span>` : ""}</div>` : ""}
    <p class="paid-ad-interpretation">Creative groups deduplicate observed public variants; they are not semantic concepts. Public libraries do not expose campaign settings, optimization events, or performance.</p>
    ${coverageNotes.length ? `<ul class="paid-ad-coverage-notes">${coverageNotes.slice(0, 3).map(note => `<li>${escapeHtml(note)}</li>`).join("")}</ul>` : ""}
    <div class="paid-ad-evidence-meta"><span>${observedAt ? `Observed ${escapeHtml(relativeTime(observedAt))}` : "Observation time unavailable"}</span>${sourceUrls.slice(0, 4).map((url, index) => `<a href="${safeUrl(typeof url === "string" ? url : url?.url || url?.source_url || "")}" target="_blank" rel="noopener">Evidence ${index + 1} ↗</a>`).join("")}</div>
  </section>`;
}

function seoSignalPanel(item = {}) {
  const rawPages = item.web?.frontier?.features?.seo_visibility_engine?.pages;
  const pages = [...new Map((Array.isArray(rawPages) ? rawPages : [])
    .filter(page => page && typeof page === "object" && page.source_url)
    .map(page => [page.source_url, page])).values()];
  const observations = Array.isArray(item.web?.frontier?.observations)
    ? item.web.frontier.observations.filter(observation => observation?.scanner_id === "dataforseo_search_visibility_v1")
    : [];
  const detailed = observations.find(observation => observation.predicate === "seo.domain_rank_overview");
  const visibility = detailed || observations.find(observation => observation.predicate === "seo.search_visibility_estimate");
  if (!pages.length && !visibility) return "";
  const scores = pages.map(page => Number(page.readiness_score)).filter(score => Number.isFinite(score) && score >= 0 && score <= 100);
  const average = scores.length ? Math.round(scores.reduce((sum, score) => sum + score, 0) / scores.length) : 0;
  const ready = pages.filter(page => page.qualifies_as_ability_proxy === true).length;
  const metaAllowed = pages.filter(page => page.meta_allows_indexing === true).length;
  const metadataComplete = pages.filter(page => page.title_present && page.description_present && page.canonical_same_company).length;
  const structured = pages.filter(page => Number(page.structured_data_blocks || 0) > 0).length;
  const value = visibility?.value && typeof visibility.value === "object" ? visibility.value : {};
  const organic = value.organic && typeof value.organic === "object" ? value.organic : {};
  const paid = value.paid && typeof value.paid === "object" ? value.paid : {};
  const topTen = detailed
    ? Number(organic.pos_1 || 0) + Number(organic.pos_2_3 || 0) + Number(organic.pos_4_10 || 0)
    : null;
  const providerHasData = visibility && value.data_available !== false;
  const label = visibility ? providerHasData ? "Provider visibility" : "No provider data" : ready ? "Strong readiness" : average >= 55 ? "Developing" : "Limited sample";
  return `<section class="drawer-section seo-signal-evidence">
    <div class="section-title-row"><h3>SEO footprint</h3><span class="state-badge state-observed">${escapeHtml(label)}</span></div>
    ${visibility ? `<div class="paid-ad-summary-grid seo-signal-summary">
      <div><small>Organic ETV</small><strong>${formatNumber(organic.etv)}</strong></div>
      <div><small>Ranked results</small><strong>${formatNumber(organic.count)}</strong></div>
      <div><small>Top-10 results</small><strong>${topTen == null ? "Bulk pass" : formatNumber(topTen)}</strong></div>
      <div><small>Paid ETV</small><strong>${formatNumber(paid.etv)}</strong></div>
    </div>
    <p class="form-note">${providerHasData ? "DataForSEO modeled US-English search estimates. They can support operating-capacity confidence, but are not first-party traffic analytics, available budget, or buyer intent." : "DataForSEO returned no modeled US-English footprint for this domain. Missing provider data is unknown and does not lower the target score."}</p>
    <div class="paid-ad-evidence-meta"><span>${visibility.last_observed_at ? `Observed ${escapeHtml(relativeTime(visibility.last_observed_at))}` : "Observation time unavailable"}</span><a href="${safeUrl(visibility.source_url)}" target="_blank" rel="noopener">DataForSEO method ↗</a></div>` : ""}
    ${pages.length ? `<div class="paid-ad-summary-grid seo-signal-summary">
      <div><small>Readiness</small><strong>${formatNumber(average)} / 100</strong></div>
      <div><small>Meta allows indexing</small><strong>${formatNumber(metaAllowed)} / ${formatNumber(pages.length)}</strong></div>
      <div><small>Complete metadata</small><strong>${formatNumber(metadataComplete)} / ${formatNumber(pages.length)}</strong></div>
      <div><small>Structured data</small><strong>${formatNumber(structured)} / ${formatNumber(pages.length)}</strong></div>
    </div>
    <p class="form-note">Bounded static page sample. Strong readiness can modestly support operating-capacity confidence; it is not a measured Google ranking or traffic estimate.</p>
    <div class="paid-ad-evidence-meta"><span>${formatNumber(pages.length)} page${pages.length === 1 ? "" : "s"} sampled</span>${pages.slice(0, 3).map((page, index) => `<a href="${safeUrl(page.source_url)}" target="_blank" rel="noopener">Page ${index + 1} ↗</a>`).join("")}</div>` : ""}
  </section>`;
}

function providerPeoplePanel(item = {}) {
  const intelligence = item.people_intelligence;
  if (!intelligence || typeof intelligence !== "object" || intelligence.status === "never_scanned") return "";
  const people = Array.isArray(intelligence.people) ? intelligence.people : [];
  const statusLabel = intelligence.status === "fresh"
    ? "Held 90d"
    : intelligence.status === "needs_more_contacts"
      ? `Needs ${formatNumber(intelligence.minimum_good_contacts || 2)} contacts`
      : humanize(intelligence.status);
  const rows = people.map(person => {
    const profile = safeUrl(person.linkedin_url || "");
    const name = escapeHtml(person.name || [person.first_name, person.last_name].filter(Boolean).join(" ") || "Professional contact");
    const nameMarkup = profile === "#" ? `<strong>${name}</strong>` : `<strong><a href="${profile}" target="_blank" rel="noopener">${name} ↗</a></strong>`;
    const email = person.email_status === "found" && person.work_email
      ? `<button class="text-button provider-person-email" type="button" data-copy-email="${escapeHtml(person.work_email)}">${escapeHtml(person.work_email)}</button>`
      : `<small>${person.email_status === "not_found" ? "Work email not found" : "Email not requested"}</small>`;
    return `<article class="provider-person-row"><div>${nameMarkup}<span>${escapeHtml(person.title || "Role unavailable")}</span></div><div><span class="badge">${escapeHtml(humanize(person.lane || "professional contact"))}</span>${email}</div></article>`;
  }).join("");
  return `<section class="drawer-section provider-people-panel">
    <div class="section-title-row"><div><h3>Provider people</h3><button type="button" class="button" data-find-company-emails="${item.candidate_id}">Find emails · $0.05 max</button><p>${intelligence.scanned_at ? `Scanned ${escapeHtml(relativeTime(intelligence.scanned_at))}` : "Scan time unavailable"}</p></div><span class="state-badge state-observed">${escapeHtml(statusLabel)}</span></div>
    <div class="provider-people-list">${rows || `<p class="form-note">No matching senior leadership or marketing/content/creative roles were returned in the bounded search.</p>`}</div>
    <p class="provider-data-disclaimer">${escapeHtml(intelligence.disclaimer || "Licensed professional data. Email verification does not establish permission or consent to contact.")}</p>
  </section>`;
}

function targetScoreBlock(item, field = "durable_fit_score") {
  const intelligence = targetIntel(item);
  if (intelligence.hydration_status !== "hydrated") return `<span class="target-unhydrated">${escapeHtml(humanize(intelligence.hydration_status))}</span>`;
  return `<span class="score target-score">${Number(intelligence[field] || 0)}</span>`;
}

function quickOpen(item) {
  return `<a class="quick-open" href="${safeUrl(item.company_url)}" target="_blank" rel="noopener" aria-label="Open ${escapeHtml(companyName(item))} website">↗</a>`;
}

function bestPublicEmail(item = {}) {
  const preferred = item.best_contact;
  if (preferred?.address && preferred.outreach_eligible !== false && !preferred.historical) return preferred.address;
  const contacts = item.contacts?.emails || [];
  const evidence = contacts.find(value => value.outreach_eligible && !value.historical);
  if (evidence?.address) return evidence.address;
  const fallback = (item.emails || []).find(value => typeof value === "string");
  return fallback || "";
}

function bestPublicPhone(item = {}) {
  const preferred = item.best_phone;
  if (preferred?.dial_number && preferred.outreach_eligible !== false && !preferred.historical) return preferred;
  const phones = item.contacts?.phones || [];
  return phones.find(value => value.outreach_eligible && !value.historical) || null;
}

function safeTel(value = "") {
  const dial = String(value).trim();
  return /^\+?\d{7,15}$/.test(dial) ? `tel:${dial}` : "";
}

function formatPhoneNumber(value = "") {
  const raw = String(value).trim();
  const digits = raw.replace(/\D/g, "");
  const national = digits.length === 11 && digits.startsWith("1") ? digits.slice(1) : digits;
  if (national.length === 10) {
    return `(${national.slice(0, 3)}) ${national.slice(3, 6)}-${national.slice(6)}`;
  }
  return raw;
}

function contactActions(item, { detailed = false } = {}) {
  const email = bestPublicEmail(item);
  const phone = bestPublicPhone(item);
  const phoneHref = safeTel(phone?.dial_number);
  const site = safeUrl(item.company_url);
  const actions = [];
  if (phoneHref) {
    const phoneLabel = formatPhoneNumber(phone.number || phone.dial_number);
    actions.push(`<a class="contact-action phone-action" href="${escapeHtml(phoneHref)}" aria-label="Call ${escapeHtml(companyName(item))} at ${escapeHtml(phoneLabel)}">${escapeHtml(phoneLabel)}</a>`);
  }
  if (site !== "#") actions.push(`<a class="contact-action site-action" href="${site}" target="_blank" rel="noopener noreferrer">Site</a>`);
  if (email) actions.push(`<button type="button" class="contact-action copy-email-action" data-copy-email="${escapeHtml(email)}">${escapeHtml(email)}</button>`);
  return `<div class="contact-actions ${detailed ? "detailed" : ""}">${actions.join("")}</div>`;
}

function exclusionButton(item) {
  const excluded = Boolean(item.excluded);
  const name = escapeHtml(companyName(item));
  return `<button class="exclude-button ${excluded ? "restore" : ""}" data-exclude="${item.candidate_id}" data-excluded="${excluded}" aria-label="${excluded ? "Restore" : "Exclude"} ${name}"Restore company" : "Exclude company"}">${excluded ? "↺" : "×"}</button>`;
}

function prospectRows(items) {
  if (!items.length) return `<tr><td colspan="8" class="loading-row">No targets.</td></tr>`;
  return items.map(item => `
    <tr class="${item.excluded ? "is-excluded" : ""}" data-prospect-id="${item.candidate_id}">
      <td class="company-cell"><strong>${escapeHtml(companyName(item))}</strong><small>${escapeHtml(targetIntel(item).lane === "referral_partner" ? "Agency partner" : humanize(targetIntel(item).scope))} · ${escapeHtml(item.canonical_domain || item.canonical_host || item.hostname)}</small></td>
      <td class="target-score-cell">${targetScoreBlock(item, "durable_fit_score")}</td>
      <td class="target-score-cell">${targetScoreBlock(item, "contact_now_score")}</td>
      <td class="target-score-cell">${targetScoreBlock(item, "founder_priority_score")}</td>
      <td class="target-action-cell">${escapeHtml(humanize(targetIntel(item).recommended_action))}</td>
      <td class="scan-failure-cell"><strong>${Number(item.scan_attempt_count || 0) ? `${Number(item.scan_failure_rate || 0).toFixed(1)}%` : "—"}</strong><small>${formatNumber(item.scan_failure_count)} / ${formatNumber(item.scan_attempt_count)} failed</small></td>
      <td class="target-reason-cell">${escapeHtml(targetWhyNow(item))}</td>
      <td class="contact-cell">${contactActions(item)}</td>
    </tr>`).join("");
}

function overviewTargetRows(items, lane, scope) {
  const rows = prospectRows(items.slice(0, 20));
  return `${rows}<tr class="overview-view-all-row"><td colspan="8"><button class="text-button overview-view-all" type="button" data-view-target="prospects" data-target-lane="${escapeHtml(lane)}" data-target-scope="${escapeHtml(scope)}">View all</button></td></tr>`;
}

function statusNumber(status, keys, fallback = 0) {
  return Number(firstValue(status || {}, keys, fallback) || 0);
}

function shortfallCopy(shortfall, status = {}) {
  const threshold = Number(shortfall?.threshold ?? firstValue(status, ["threshold.target_score", "threshold"], 70));
  const best = Number(shortfall?.best_score ?? shortfall?.highest_score ?? firstValue(status, ["best_target_score", "threshold.maximum_score", "maximum_target_score", "highest_score"], 0));
  const count = Number(shortfall?.at_or_above ?? statusNumber(status, ["threshold_count", "threshold.at_or_above", "score_70_plus", "targets_at_or_above_threshold", "targets_at_or_above_70"], 0));
  if (count) return `${formatNumber(count)} target${count === 1 ? "" : "s"} ≥${threshold}`;
  if (best) return `Best ${best} · ${Math.max(0, threshold - best)} below ${threshold}`;
  return `0 targets ≥${threshold}`;
}

function renderTargetCoverage(status = {}, shortfall = null, root = document) {
  const discovered = statusNumber(status, ["candidate_inventory", "inventory_candidates", "total_candidates", "candidate_count", "discovered_candidates", "inventory.candidates"], 0);
  const inspected = statusNumber(status, ["inspected_candidates", "inspected", "inventory.inspected"], 0);
  const hydrated = statusNumber(status, ["hydrated_latest_candidates", "hydrated_candidates", "hydrated_latest", "inventory.hydrated"], 0);
  const uninspected = statusNumber(status, ["uninspected_candidates", "uninspected", "backlog", "inventory.uninspected"], Math.max(0, discovered - inspected));
  const coverageRaw = firstValue(status, ["coverage", "coverage_ratio", "projection_coverage"], discovered ? hydrated / discovered : 0);
  const coverage = Number(coverageRaw || 0);
  const coverageLabel = `${coverage <= 1 ? (coverage * 100).toFixed(1) : coverage.toFixed(1)}%`;
  const setText = (selector, value) => { const node = $(selector, root); if (node) node.textContent = value; };
  setText("#metric-target-discovered", formatNumber(discovered));
  setText("#metric-target-inspected", formatNumber(inspected));
  setText("#metric-target-hydrated", formatNumber(hydrated));
  setText("#metric-target-uninspected", formatNumber(uninspected));
  setText("#target-coverage-title", `${coverageLabel} hydrated`);
  setText("#target-coverage-copy", `${formatNumber(hydrated)} projected · ${formatNumber(inspected)} inspected · ${formatNumber(uninspected)} uninspected`);
  setText("#target-shortfall", shortfallCopy(shortfall, status));
}

function renderOverview(data) {
  state.dashboard = data;
  state.tags = data.tags || [];
  state.targetStatus = data.target_intelligence || {};
  state.targetShortfall = data.target_shortfall || null;
  renderTargetCoverage(state.targetStatus, state.targetShortfall);
  $("#buyer-target-body").innerHTML = overviewTargetRows((data.top_buyer_targets || []).filter(item => targetIntel(item).scope === "direct"), "buyer", "direct");
  $("#referral-target-body").innerHTML = overviewTargetRows(data.top_referral_partners || [], "referral_partner", "all");
  syncTargetSortHeaders($("#view-overview"), state.overviewSort);
}

function syncTargetSortHeaders(root, sort) {
  if (!root) return;
  $$("[data-target-sort]", root).forEach(button => {
    const active = button.dataset.targetSort === sort;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function metricRow(label, value, note = "") {
  return `<div class="command-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ""}</div>`;
}

function renderAccountDashboard(data) {
  state.accountDashboard = data;
  const metrics = data.metrics || data;
  const states = data.qualification || data.qualification_states || data.state_counts || metrics.qualification_states || {};
  const lifecycle = data.pipeline || data.lifecycle || data.lifecycle_counts || metrics.lifecycle || {};
  const outcomes = data.outcomes || metrics.outcomes || metrics;
  const total = firstValue(data, ["metrics.total", "metrics.total_accounts", "metrics.accounts", "total_accounts", "accounts", "total"], 0);
  const pursue = firstValue(data, ["metrics.pursue", "qualification.pursue", "qualification_states.pursue", "state_counts.pursue", "pursue"], 0);
  const discoveries = firstValue(data, ["metrics.discovery_calls", "outcomes.discovery_calls", "discovery_calls", "lifecycle.discovery"], 0);
  const won = firstValue(data, ["metrics.wins", "metrics.won", "outcomes.wins", "outcomes.won", "won", "pipeline.won", "lifecycle.won"], 0);
  const renewed = firstValue(data, ["metrics.renewals", "metrics.renewed", "outcomes.renewals", "outcomes.renewed", "renewed"], 0);
  const margin = firstValue(data, ["metrics.contribution_margin", "outcomes.average_contribution_margin", "outcomes.contribution_margin", "contribution_margin"], null);
  $("#metric-accounts").textContent = formatNumber(total);
  $("#metric-pursue").textContent = formatNumber(pursue);
  $("#metric-discovery").textContent = formatNumber(discoveries);
  $("#metric-won").textContent = formatNumber(won);
  $("#metric-renewed").textContent = formatNumber(renewed);
  $("#metric-margin").textContent = money(margin);

  const needsResearch = firstValue(data, ["qualification.needs_research", "qualification_states.needs_research", "state_counts.needs_research"], states.needs_research || 0);
  const disqualified = firstValue(data, ["qualification.disqualified", "qualification_states.disqualified", "state_counts.disqualified"], states.disqualified || 0);
  const outreach = lifecycle.outreach || 0;
  const proposal = lifecycle.proposal || 0;
  $("#account-funnel").innerHTML = [
    metricRow("Curated", formatNumber(total)),
    metricRow("Needs research", formatNumber(needsResearch)),
    metricRow("Pursue", formatNumber(pursue)),
    metricRow("Disqualified", formatNumber(disqualified)),
    metricRow("Outreach", formatNumber(outreach)),
    metricRow("Scan", formatNumber(discoveries)),
    metricRow("Proposal", formatNumber(proposal)),
    metricRow("Won", formatNumber(won)),
  ].join("");

  const closeRate = firstValue(data, ["outcomes.close_rate", "metrics.close_rate", "close_rate"], null);
  const renewalRate = firstValue(data, ["outcomes.renewal_rate", "metrics.renewal_rate", "renewal_rate"], null);
  const revisionRounds = firstValue(data, ["outcomes.average_revision_rounds", "metrics.average_revision_rounds"], null);
  const revisionHours = firstValue(data, ["outcomes.average_revision_hours", "metrics.average_revision_hours"], null);
  const burdenCounts = firstValue(data, ["outcomes.delivery_burden_counts"], {});
  const deliverySample = firstValue(data, ["outcomes.delivery_sample_size", "metrics.delivery_sample_size"], 0);
  const averageMargin = firstValue(data, ["outcomes.average_contribution_margin"], null);
  const discoveryCount = Number(firstValue(data, ["calibration.discovery_calls", "outcomes.discovery_calls", "discovery_calls"], discoveries));
  const renewalDecisions = Number(firstValue(data, ["calibration.renewal_decisions", "outcomes.renewal_decisions", "renewal_decisions"], 0));
  const calibrated = discoveryCount >= 20 && renewalDecisions >= 3;
  const foundingCore = firstValue(data, ["founding_mix.paid_growth_consumer_digital"], 0);
  const foundingAgencies = firstValue(data, ["founding_mix.agency_buyers"], 0);
  const foundingBuyers = firstValue(data, ["founding_mix.primary_buyers"], foundingCore + foundingAgencies);
  const foundingSaas = firstValue(data, ["founding_mix.saas_ai_adjacency"], 0);
  const warmPlan = firstValue(data, ["source_plan.warm_network"], {});
  const referralPlan = firstValue(data, ["source_plan.specialist_referral"], {});
  const outboundPlan = firstValue(data, ["source_plan.targeted_outbound"], {});
  const onboarding = firstValue(data, ["onboarding"], {});
  $("#account-outcomes").innerHTML = [
    metricRow("Close rate", percentage(closeRate)),
    metricRow("Renewal rate", percentage(renewalRate)),
    metricRow("Founding buyer mix", `${formatNumber(foundingBuyers)} buyers · ${formatNumber(foundingAgencies)} agencies · ${formatNumber(foundingSaas)} SaaS/AI`),
    metricRow("Warm network plan", `${formatNumber(warmPlan.accounts || 0)}/${formatNumber(warmPlan.target_accounts || 60)} accounts · ${formatNumber(warmPlan.wins || 0)}/${formatNumber(warmPlan.target_wins || 5)} wins`),
    metricRow("Specialist referral plan", `${formatNumber(referralPlan.accounts || 0)}/${formatNumber(referralPlan.target_accounts || 40)} accounts · ${formatNumber(referralPlan.wins || 0)}/${formatNumber(referralPlan.target_wins || 3)} wins`),
    metricRow("Qualified outbound plan", `${formatNumber(outboundPlan.accounts || 0)}/${formatNumber(outboundPlan.target_accounts || 150)} accounts · ${formatNumber(outboundPlan.wins || 0)}/${formatNumber(outboundPlan.target_wins || 2)} wins`),
    metricRow("Onboarding capacity", `${formatNumber(onboarding.started_this_week || 0)}/${formatNumber(onboarding.weekly_maximum || 2)} this week`, onboarding.over_capacity ? "Over capacity" : onboarding.at_capacity ? "At capacity" : ""),
    metricRow("Revision burden", revisionRounds == null ? "—" : `${Number(revisionRounds).toFixed(1)} rounds · ${Number(revisionHours || 0).toFixed(1)}h`, `${formatNumber(deliverySample)} accounts`),
    metricRow("Delivery burden", deliverySample ? `${formatNumber(burdenCounts.high || 0)} high · ${formatNumber(burdenCounts.uneconomic || 0)} uneconomic` : "—"),
    metricRow("Average contribution margin", money(averageMargin)),
    metricRow("Calibration", calibrated ? "Available" : "Deferred", `${formatNumber(discoveryCount)}/20 scan calls · ${formatNumber(renewalDecisions)}/3 renewals`),
  ].join("");
}

function sourceYieldRows(items) {
  if (!items.length) return `<tr><td colspan="5" class="loading-row">No sources.</td></tr>`;
  return items.map(item => `<tr>
    <td class="source-name"><strong>${escapeHtml(item.apex)}</strong><small>${escapeHtml(item.category)}</small></td>
    <td><span class="source-rank">${Number(item.opportunity_score || 0)}</span></td>
    <td class="source-count">${formatNumber(item.inspected_count)}</td>
    <td class="source-count">${Number(item.live_rate || 0).toFixed(1)}%</td>
    <td class="source-count">${Number(item.high_priority_yield || 0).toFixed(1)}%</td>
  </tr>`).join("");
}

function readTargetOverviewCache() {
  try {
    const cached = JSON.parse(localStorage.getItem(TARGET_OVERVIEW_CACHE_KEY) || "null");
    return cached?.payload && typeof cached.payload === "object" ? cached.payload : null;
  } catch {
    try { localStorage.removeItem(TARGET_OVERVIEW_CACHE_KEY); } catch {}
    return null;
  }
}

function writeTargetOverviewCache(payload) {
  try {
    localStorage.setItem(TARGET_OVERVIEW_CACHE_KEY, JSON.stringify({
      cached_at: new Date().toISOString(),
      payload,
    }));
  } catch {
    // Private browsing and storage quotas must never block the live overview.
  }
}

function invalidateTargetOverviewCache() {
  try { localStorage.removeItem(TARGET_OVERVIEW_CACHE_KEY); } catch {}
}

function readSourcesViewCache() {
  try {
    const cached = JSON.parse(localStorage.getItem(SOURCES_VIEW_CACHE_KEY) || "null");
    return cached && typeof cached === "object" ? cached : null;
  } catch {
    try { localStorage.removeItem(SOURCES_VIEW_CACHE_KEY); } catch {}
    return null;
  }
}

function writeSourcesViewCache(key, payload) {
  try {
    const cached = readSourcesViewCache() || {};
    localStorage.setItem(SOURCES_VIEW_CACHE_KEY, JSON.stringify({
      ...cached,
      cached_at: new Date().toISOString(),
      [key]: payload,
    }));
  } catch {
    // Private browsing and storage quotas must never block live source data.
  }
}

async function fetchTargetOverview() {
  const sort = encodeURIComponent(state.overviewSort);
  try { return await api(`/api/targets/overview?limit=20&include_status=false&sort=${sort}`); }
  catch (overviewError) {
    const [buyers, referrals] = await Promise.all([
      api(`/api/targets?lane=buyer&scope=direct&limit=20&sort=${sort}`),
      api(`/api/targets?lane=referral_partner&scope=all&limit=20&sort=${sort}`),
    ]);
    return { generation: buyers.generation || referrals.generation || "", top_buyer_targets: buyers.items || [], top_referral_partners: referrals.items || [] };
  }
}

async function loadDashboard({ useCached = false } = {}) {
  const requestId = ++state.dashboardRequestId;
  const cachedCandidate = useCached ? readTargetOverviewCache() : null;
  const cached = cachedCandidate?.generation && cachedCandidate?._sort === state.overviewSort ? cachedCandidate : null;
  if (cached) renderOverview(cached);
  const statusRequest = api("/api/targets/status").catch(error => ({ _error: error }));
  try {
    const overview = await fetchTargetOverview();
    if (requestId !== state.dashboardRequestId) return;
    renderOverview({
      ...overview,
      target_intelligence: cached?.target_intelligence || {},
      target_shortfall: cached?.target_shortfall || null,
    });
    try {
      const targetStatus = await statusRequest;
      if (requestId !== state.dashboardRequestId) return;
      if (targetStatus._error) throw targetStatus._error;
      const enriched = {
        ...overview,
        generation: overview.generation || targetStatus.generation || "",
        _sort: state.overviewSort,
        target_intelligence: targetStatus.status || {},
        target_shortfall: targetStatus.shortfall || null,
      };
      renderOverview(enriched);
      writeTargetOverviewCache(enriched);
    } catch (statusError) {
      showToast(statusError.message, "error");
      writeTargetOverviewCache({ ...overview, _sort: state.overviewSort });
    }
  } catch (error) {
    showToast(error.message, "error");
    if (cached) return;
    $("#buyer-target-body").innerHTML = `<tr><td colspan="5" class="loading-row">Could not load buyer targets.</td></tr>`;
    $("#referral-target-body").innerHTML = `<tr><td colspan="5" class="loading-row">Could not load agency targets.</td></tr>`;
    $("#target-coverage-title").textContent = "Target intelligence unavailable";
    $("#target-coverage-copy").textContent = "";
  }
}

function optionList(items, selected = "", { empty = "" } = {}) {
  const head = empty ? `<option value="">${escapeHtml(empty)}</option>` : "";
  return head + items.map(value => `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(humanize(value))}</option>`).join("");
}

function accountParams({ exportAll = false } = {}) {
  const params = new URLSearchParams();
  Object.entries(state.accountFilters).forEach(([key, value]) => {
    if (value !== "" && value != null) params.set(key, String(value));
  });
  if (!exportAll) {
    params.set("limit", String(RESULT_PAGE_SIZE));
    if (state.accountOffset) params.set("offset", String(state.accountOffset));
  }
  return params;
}

function accountQualification(item) {
  return item.qualification || {};
}

function accountScore(item) {
  return Number(item.qualification_score ?? accountQualification(item).total_score ?? 0);
}

function accountState(item) {
  return item.qualification_state || accountQualification(item).state || "needs_research";
}

function gateSummary(item) {
  const gates = accountQualification(item).gates || {};
  const values = QUALIFICATION_GATES.map(name => gates[name] || "unknown");
  return {
    met: values.filter(value => value === "met").length,
    failed: values.filter(value => value === "not_met").length,
    unknown: values.filter(value => value === "unknown").length,
  };
}

function accountTargetCell(item) {
  const intelligence = item.target_intelligence;
  if (!intelligence) return `<span class="target-unhydrated">Unlinked</span>`;
  const target = targetIntel(item);
  if (target.hydration_status !== "hydrated") return `<span class="target-unhydrated">${escapeHtml(humanize(target.hydration_status))}</span>`;
  return `<div class="account-machine-score">${targetScoreBlock(item, "durable_fit_score")}<small>${target.principal_signals_met}/3 signals · ${escapeHtml(humanize(target.scope))}</small></div>`;
}

function accountRows(items) {
  if (!items.length) return `<tr><td colspan="9" class="loading-row">No accounts.</td></tr>`;
  return items.map(item => {
    const gates = gateSummary(item);
    const website = item.website || "";
    const source = item.lead_source || "unknown";
    const relationship = item.relationship_type || "buyer";
    return `<tr data-account-id="${escapeHtml(item.id)}">
      <td class="company-cell"><button class="account-name-button" type="button"><strong>${escapeHtml(companyName(item))}</strong><small>${escapeHtml(website.replace(/^https?:\/\//, ""))}</small></button></td>
      <td><strong>${escapeHtml(humanize(source))}</strong><small class="cell-meta">${escapeHtml(item.referrer || item.cohort || "prototype-v1")}</small></td>
      <td>${stateBadge(relationship)}</td>
      <td><strong>${escapeHtml(humanize(item.segment || "unknown"))}</strong></td>
      <td>${accountTargetCell(item)}</td>
      <td><span class="gate-count ${gates.failed ? "failed" : gates.unknown ? "unknown" : "complete"}">${gates.met}/6</span><small class="cell-meta">${gates.failed ? `${gates.failed} failed` : gates.unknown ? `${gates.unknown} unknown` : "all met"}</small></td>
      <td><span class="score strategic-score">${accountScore(item)}</span></td>
      <td>${stateBadge(accountState(item))}</td>
      <td><div class="row-actions"><span class="lifecycle-label">${escapeHtml(humanize(item.lifecycle || "research"))}</span>${website ? `<a class="quick-open" href="${safeUrl(website)}" target="_blank" rel="noopener" aria-label="Open ${escapeHtml(companyName(item))} website">↗</a>` : ""}<button class="row-action" aria-label="Open ${escapeHtml(companyName(item))} account workspace">›</button></div></td>
    </tr>`;
  }).join("");
}

function accountsShell() {
  const filters = state.accountFilters;
  $("#view-accounts").innerHTML = `
    <div class="section-intro section-intro-actions"><div class="section-actions"><button class="button" id="import-accounts">Import CSV</button><button class="button primary" id="add-account">Add account</button></div></div>
    <div class="toolbar account-toolbar">
      <input class="toolbar-input" id="account-cohort" aria-label="Filter by cohort" value="${escapeHtml(filters.cohort)}" placeholder="Cohort" />
      <select id="account-relationship" aria-label="Filter by relationship">${optionList(["buyer", "referral_partner"], filters.relationship_type, { empty: "All relationships" })}</select>
      <select id="account-segment" aria-label="Filter by segment">${optionList(ACCOUNT_SEGMENTS, filters.segment, { empty: "All segments" })}</select>
      <select id="account-source" aria-label="Filter by lead source">${optionList(["past_client", "founder_investor_network", "specialist_referral", "targeted_outbound", "other"], filters.lead_source, { empty: "All sources" })}</select>
      <select id="account-state" aria-label="Filter by qualification state">${optionList(QUALIFICATION_STATES, filters.qualification_state, { empty: "All states" })}</select>
      <select id="account-lifecycle" aria-label="Filter by lifecycle">${optionList(ACCOUNT_LIFECYCLES, filters.lifecycle, { empty: "All lifecycle" })}</select>
      <input class="toolbar-input score-filter" id="account-min-score" aria-label="Minimum strategic score" type="number" min="0" max="100" value="${escapeHtml(filters.min_score)}" placeholder="Min score" />
      <button class="button" id="export-accounts">Accounts CSV</button><button class="button" id="export-evidence">Evidence CSV</button><button class="button" id="export-outcomes">Outcomes CSV</button><button class="button" id="export-pipeline">Pipeline brief</button>
      <span class="result-count" id="account-count">Loading…</span>
    </div>
    <section class="panel"><div class="table-wrap"><table class="account-table"><thead><tr><th>Company</th><th>Source</th><th>Relationship</th><th>Segment</th><th>Machine target</th><th>Human gates</th><th>Human score</th><th>Human state</th><th>Lifecycle</th></tr></thead><tbody id="account-body"><tr><td colspan="9" class="loading-row">${loadingIndicator("Loading accounts…")}</td></tr></tbody></table></div><div class="pagination"><span id="account-range">—</span><div class="pagination-actions"><button class="button" id="account-previous" type="button">Previous</button><button class="button" id="account-next" type="button">Next</button><button class="button" id="refresh-accounts" type="button">Refresh</button></div></div></section>`;

  const filterBindings = [
    ["#account-cohort", "cohort"], ["#account-relationship", "relationship_type"], ["#account-segment", "segment"],
    ["#account-source", "lead_source"], ["#account-state", "qualification_state"], ["#account-lifecycle", "lifecycle"], ["#account-min-score", "min_score"],
  ];
  filterBindings.forEach(([selector, key]) => {
    $(selector).addEventListener("change", event => { state.accountFilters[key] = event.target.value; state.accountOffset = 0; loadAccounts(); });
  });
  $("#account-previous").addEventListener("click", () => { state.accountOffset = Math.max(0, state.accountOffset - RESULT_PAGE_SIZE); loadAccounts(); });
  $("#account-next").addEventListener("click", () => { state.accountOffset += RESULT_PAGE_SIZE; loadAccounts(); });
  $("#refresh-accounts").addEventListener("click", loadAccounts);
  $("#add-account").addEventListener("click", () => $("#account-modal").showModal());
  $("#import-accounts").addEventListener("click", () => $("#account-import-modal").showModal());
  $("#export-accounts").addEventListener("click", () => downloadPath(`/api/accounts/export.csv?${accountParams({ exportAll: true })}`));
  $("#export-evidence").addEventListener("click", () => downloadPath(`/api/accounts/evidence.csv?${accountParams({ exportAll: true })}`));
  $("#export-outcomes").addEventListener("click", () => downloadPath(`/api/accounts/outcomes.csv?cohort=${encodeURIComponent(state.accountFilters.cohort)}`));
  $("#export-pipeline").addEventListener("click", () => downloadPath(`/api/accounts/pipeline.md?cohort=${encodeURIComponent(state.accountFilters.cohort)}`));
}

function downloadPath(path) {
  const link = document.createElement("a");
  link.href = path;
  document.body.append(link);
  link.click();
  link.remove();
}

async function loadAccounts() {
  if (!$("#account-body")) return;
  const requestId = ++state.accountRequestId;
  $("#account-body").innerHTML = `<tr><td colspan="9" class="loading-row">${loadingIndicator("Loading accounts…")}</td></tr>`;
  try {
    const result = await api(`/api/accounts?${accountParams()}`);
    if (requestId !== state.accountRequestId) return;
    state.accounts = result.items || [];
    state.accountTotal = Number(result.total || 0);
    if (!state.accounts.length && state.accountOffset > 0 && state.accountTotal > 0) {
      state.accountOffset = Math.floor((state.accountTotal - 1) / RESULT_PAGE_SIZE) * RESULT_PAGE_SIZE;
      return loadAccounts();
    }
    $("#account-body").innerHTML = accountRows(state.accounts);
    $("#account-count").textContent = `${formatNumber(state.accountTotal)} accounts`;
    setPageSearchStatus(state.pageSearch.trim() ? `${formatNumber(state.accountTotal)} matches` : "");
    const first = state.accountOffset + 1;
    const last = state.accountOffset + state.accounts.length;
    $("#account-range").textContent = state.accounts.length ? `Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(state.accountTotal)}` : "No accounts";
    $("#account-previous").disabled = state.accountOffset === 0;
    $("#account-next").disabled = last >= state.accountTotal;
  } catch (error) {
    if (requestId !== state.accountRequestId) return;
    showToast(error.message, "error");
    $("#account-body").innerHTML = `<tr><td colspan="9" class="loading-row">Could not load accounts.</td></tr>`;
  }
}

function arrayFromLines(value = "") {
  return String(value).split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

function linesFromArray(value) {
  return Array.isArray(value) ? value.join("\n") : "";
}

function numberOrNull(value) {
  if (value === "" || value == null) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function isoOrNull(value) {
  return value ? new Date(value).toISOString() : null;
}

function dateInput(value) {
  return value ? String(value).slice(0, 10) : "";
}

function truthSelect(name, selected = "unknown", label = humanize(name)) {
  return `<label class="field compact-field"><span>${escapeHtml(label)}</span><select name="${escapeHtml(name)}">${optionList(TRUTH_STATES, selected)}</select></label>`;
}

function scoreInput(name, value = 0) {
  const maximum = DIMENSION_LIMITS[name];
  return `<label class="score-input"><span>${escapeHtml(humanize(name))}<small>0–${maximum}</small></span><input name="${escapeHtml(name)}" type="number" min="0" max="${maximum}" step="1" value="${Number(value || 0)}" /></label>`;
}

function accountTimestamp(value) {
  return value ? absoluteDate(value) : "Not recorded";
}

function unwrapAccount(payload) {
  return payload?.account || payload?.item || payload;
}

function accountActivity(account) {
  const events = [...(account.activities || account.activity || [])];
  if (account.created_at) events.push({ activity_type: "account_created", summary: "Curated account created", occurred_at: account.created_at });
  if (account.discovery?.held_at) events.push({ activity_type: "discovery_review", summary: account.discovery.completed ? "Scan review completed" : "Scan call recorded", occurred_at: account.discovery.held_at });
  if (account.outcome?.proposal_sent_at) events.push({ activity_type: "proposal_sent", summary: `${money(account.outcome.proposal_monthly_value)} monthly proposal`, occurred_at: account.outcome.proposal_sent_at });
  if (account.outcome?.won_at) events.push({ activity_type: "won", summary: "Account won", occurred_at: account.outcome.won_at });
  if (account.outcome?.lost_at) events.push({ activity_type: "lost", summary: account.outcome.lost_reason || "Account lost", occurred_at: account.outcome.lost_at });
  if (account.delivery?.renewal_decided_at) events.push({ activity_type: "renewal_decision", summary: humanize(account.delivery.renewal_decision), occurred_at: account.delivery.renewal_decided_at });
  return events.filter(event => event.occurred_at).sort((left, right) => new Date(right.occurred_at) - new Date(left.occurred_at));
}

function renderAccountWorkspace(account) {
  state.activeAccount = account;
  const qualification = account.qualification || {};
  const dimensions = qualification.dimensions || {};
  const signals = qualification.principal_signals || {};
  const gates = qualification.gates || {};
  const buyer = qualification.buyer || {};
  const capacityMap = account.capacity_map || {};
  const mapBuyer = capacityMap.economic_buyer || buyer;
  const discovery = account.discovery || {};
  const approver = discovery.approver || {};
  const outcome = account.outcome || {};
  const delivery = account.delivery || {};
  const claims = account.claims || [];
  const qualificationState = accountState(account);
  const activities = accountActivity(account);
  const machineTarget = account.target_intelligence ? targetIntel(account) : null;
  $("#account-workspace-content").innerHTML = `
    <header class="account-workspace-head">
      <div><p class="eyebrow">${escapeHtml(account.cohort || "prototype-v1")} · ${escapeHtml(humanize(account.relationship_type || "buyer"))}</p><h2>${escapeHtml(companyName(account))}</h2><a href="${safeUrl(account.website)}" target="_blank" rel="noopener">${escapeHtml(account.website)} ↗</a></div>
      <div class="account-workspace-actions"><span class="engine-pill">${escapeHtml(humanize(account.lifecycle || "research"))}</span><button class="button primary" id="account-review-evidence">Review evidence</button><button class="icon-button" id="close-account-workspace" aria-label="Close deal details">×</button></div>
    </header>
    <nav class="workspace-nav" aria-label="Account workspace sections"><a href="#account-profile">Profile</a><a href="#account-qualification">Qualification</a><a href="#account-capacity-map">Capacity map</a><a href="#account-discovery">Scan & outcomes</a><a href="#account-delivery">Delivery & renewal</a><a href="#account-evidence">Evidence</a><a href="#account-activity">Activity</a></nav>
    <div class="account-workspace-body">
      <section class="workspace-section" id="account-profile"><div class="workspace-section-head"><h3>Profile & relationship</h3></div>
        <form id="account-profile-form" class="workspace-form form-grid">
          <label class="field"><span>Company</span><input name="company" required value="${escapeHtml(account.company)}" /></label><label class="field"><span>Website</span><input name="website" type="url" required value="${escapeHtml(account.website)}" /></label>
          <label class="field"><span>Cohort</span><input name="cohort" value="${escapeHtml(account.cohort || "prototype-v1")}" /></label><label class="field"><span>Segment</span><select name="segment">${optionList(ACCOUNT_SEGMENTS, account.segment)}</select></label>
          <label class="field"><span>Relationship</span><select name="relationship_type">${optionList(["buyer", "referral_partner"], account.relationship_type)}</select></label><label class="field"><span>Lead source</span><select name="lead_source">${optionList(["past_client", "founder_investor_network", "specialist_referral", "targeted_outbound", "other"], account.lead_source)}</select></label>
          <label class="field"><span>Introduction status</span><select name="intro_status">${optionList(["none", "requested", "opted_in", "introduced", "declined"], account.intro_status)}</select></label><label class="field"><span>Lifecycle</span><input value="${escapeHtml(humanize(account.lifecycle || "research"))}" readonly /></label>
          <label class="field form-wide"><span>Referrer / context</span><input name="referrer" value="${escapeHtml(account.referrer || "")}" /></label><label class="field form-wide"><span>Account notes</span><textarea name="notes" rows="3">${escapeHtml(account.notes || "")}</textarea></label><div class="form-actions form-wide"><button class="button primary" type="submit">Save profile</button></div>
        </form>
      </section>

      <section class="workspace-section account-target-projection"><div class="workspace-section-head"><h3>Linked target intelligence</h3>${machineTarget ? `<span class="coverage-version">${escapeHtml(machineTarget.version)}</span>` : ""}</div>
        ${machineTarget ? `<div class="account-target-summary"><div class="account-target-score"><small>Durable fit</small>${targetScoreBlock(account, "durable_fit_score")}<span>${escapeHtml(humanize(machineTarget.lane))} · ${escapeHtml(humanize(machineTarget.scope))}</span></div>${targetAxes(account)}${targetSignals(account, { detailed: true })}</div><div class="account-target-provenance"><span>${machineTarget.observed_claim_count} observed</span><span>${machineTarget.inferred_claim_count} inferred</span><span>${escapeHtml(targetFreshness(account))}</span><span>${escapeHtml(targetLeadingGap(account))}</span></div>` : `<p class="empty">Not linked.</p>`}
      </section>

      <section class="workspace-section" id="account-qualification"><div class="workspace-section-head"><div><p class="eyebrow">Creative-capacity-v1</p><h3>Qualification</h3></div><div class="qualification-summary">${stateBadge(qualificationState)}<strong>${accountScore(account)}/100</strong></div></div>
        <div class="epistemic-legend"><span>${epistemicBadge("observed", true)}</span><span>${epistemicBadge("inferred", false)}</span><span>${epistemicBadge("unknown", false)}</span></div>
        <form id="qualification-form" class="workspace-form">
          <fieldset><legend>Weighted dimensions <small>25 / 20 / 25 / 15 / 15</small></legend><div class="dimension-grid">${Object.keys(DIMENSION_LIMITS).map(name => scoreInput(name, dimensions[name])).join("")}</div></fieldset>
          <fieldset><legend>Explicit penalties</legend><div class="penalty-grid">${[0, 1, 2, 3, 4].map(index => { const penalty = qualification.penalties?.[index] || {}; return `<div class="penalty-row"><label class="field"><span>Name</span><input name="penalty_name_${index}" value="${escapeHtml(penalty.name || "")}" /></label><label class="field"><span>Points</span><input name="penalty_points_${index}" type="number" min="1" max="15" value="${penalty.points ?? ""}" /></label><label class="field"><span>Reason</span><input name="penalty_reason_${index}" value="${escapeHtml(penalty.reason || "")}" /></label></div>`; }).join("")}</div></fieldset>
          <fieldset><legend>Three principal signals</legend><div class="truth-grid">${PRINCIPAL_SIGNALS.map(name => truthSelect(name, signals[name], humanize(name))).join("")}</div><div class="coverage-row">${PRINCIPAL_SIGNALS.map(name => `<span class="coverage-chip ${qualification.evidence_coverage?.[name] ? "covered" : "missing"}">${escapeHtml(humanize(name))}: ${qualification.evidence_coverage?.[name] ? "verified" : "missing verified observation"}</span>`).join("")}</div></fieldset>
          <fieldset><legend>Six hard gates</legend><div class="truth-grid">${QUALIFICATION_GATES.map(name => truthSelect(name, gates[name], humanize(name))).join("")}</div></fieldset>
          <fieldset><legend>Buyer, timing, and exactly three asks</legend><div class="form-grid"><label class="field"><span>Buyer name</span><input name="buyer_name" value="${escapeHtml(buyer.name || "")}" /></label><label class="field"><span>Buyer role</span><input name="buyer_role" value="${escapeHtml(buyer.role || "")}" placeholder="Head of Growth" /></label><label class="field"><span>Confidence</span><select name="confidence">${optionList(["low", "medium", "high"], qualification.confidence)}</select></label><label class="field form-wide"><span>Why now</span><textarea name="why_now" rows="3">${escapeHtml(qualification.why_now || "")}</textarea></label>${[0, 1, 2].map(index => `<label class="field"><span>First-month ask ${index + 1}</span><input name="ask_${index}" value="${escapeHtml(qualification.first_month_asks?.[index] || "")}" /></label>`).join("")}</div></fieldset>
          ${qualification.blockers?.length ? `<div class="blocker-panel"><strong>Pursuit blockers</strong><ul>${qualification.blockers.map(blocker => `<li>${escapeHtml(blocker)}</li>`).join("")}</ul></div>` : ""}
          <div class="form-actions"><button class="button primary" type="submit">Save and recompute</button></div>
        </form>
      </section>

      <section class="workspace-section" id="account-capacity-map"><div class="workspace-section-head"><h3>Creative Capacity Map</h3>${stateBadge(capacityMap.ready ? "ready" : "not_ready")}<a class="button" href="/api/accounts/${encodeURIComponent(account.id)}/capacity-map.md">Download Markdown</a></div>
        <form id="capacity-map-form" class="workspace-form form-grid">
          <label class="field form-wide"><span>Current promotion <small>one item per line</small></span><textarea name="current_promotion" rows="3">${escapeHtml(linesFromArray(capacityMap.current_promotion))}</textarea></label><label class="field"><span>Distinct concept count</span><input name="distinct_concept_count" type="number" min="0" value="${capacityMap.distinct_concept_count ?? ""}" /></label>
          <label class="field form-wide"><span>Observed concept notes <small>facts, one per line</small></span><textarea name="concept_notes" rows="3">${escapeHtml(linesFromArray(capacityMap.concept_notes))}</textarea></label><label class="field form-wide"><span>Observed demand <small>facts, one per line</small></span><textarea name="observed_demand" rows="3">${escapeHtml(linesFromArray(capacityMap.observed_demand))}</textarea></label>
          <label class="field form-wide inference-field"><span>Production-gap hypothesis <small>explicit inference</small></span><textarea name="production_gap_hypothesis" rows="3">${escapeHtml(capacityMap.production_gap_hypothesis || "")}</textarea></label>
          ${[0, 1, 2].map(index => `<label class="field"><span>Credible ask ${index + 1}</span><input name="map_ask_${index}" value="${escapeHtml(capacityMap.first_month_asks?.[index] || qualification.first_month_asks?.[index] || "")}" /></label>`).join("")}
          <label class="field form-wide"><span>Month-one queue <small>one deliverable per line</small></span><textarea name="first_month_queue" rows="4">${escapeHtml(linesFromArray(capacityMap.first_month_queue))}</textarea></label><label class="field"><span>Economic buyer name</span><input name="map_buyer_name" value="${escapeHtml(mapBuyer.name || "")}" /></label><label class="field"><span>Economic buyer role</span><input name="map_buyer_role" value="${escapeHtml(mapBuyer.role || "")}" /></label><label class="field form-wide"><span>Rights / AI objection</span><textarea name="rights_ai_objection" rows="3">${escapeHtml(capacityMap.rights_ai_objection || "")}</textarea></label><div class="form-actions form-wide"><button class="button primary" type="submit">Save map</button></div>
        </form>
      </section>

      <section class="workspace-section" id="account-discovery"><div class="workspace-section-head"><h3>Scan & outcomes</h3></div>
        <form id="discovery-form" class="workspace-form discovery-form"><fieldset><legend>Discovery review</legend><div class="form-grid"><label class="field"><span>Discovery result</span><select name="discovery_outcome">${optionList(["unknown", "qualified", "not_fit", "no_budget", "not_now"], discovery.outcome || "unknown")}</select></label><label class="field"><span>Research time (minutes)</span><input name="research_minutes" type="number" min="0" step="0.1" value="${discovery.research_minutes || 0}" /></label><label class="field form-wide"><span>Result reason</span><textarea name="outcome_reason">${escapeHtml(discovery.outcome_reason || "")}</textarea></label><label class="field"><span>Recurring need confirmed</span><select name="confirmed_recurring_need"><option value="">Unknown</option><option value="true" ${discovery.confirmed_recurring_need === true ? "selected" : ""}>Yes</option><option value="false" ${discovery.confirmed_recurring_need === false ? "selected" : ""}>No</option></select></label><label class="field"><span>Held at</span><input name="held_at" type="datetime-local" value="${localDateTimeValue(discovery.held_at)}" /></label><label class="check-field"><input name="completed" type="checkbox" ${discovery.completed ? "checked" : ""} /><span>Review completed</span></label><label class="field form-wide"><span>Creative backlog <small>one item per line</small></span><textarea name="creative_backlog">${escapeHtml(linesFromArray(discovery.creative_backlog))}</textarea></label><label class="field"><span>Must ship in 30 days</span><textarea name="must_ship_30_days">${escapeHtml(linesFromArray(discovery.must_ship_30_days))}</textarea></label><label class="field"><span>Must ship in 60 days</span><textarea name="must_ship_60_days">${escapeHtml(linesFromArray(discovery.must_ship_60_days))}</textarea></label><label class="field"><span>Must ship in 90 days</span><textarea name="must_ship_90_days">${escapeHtml(linesFromArray(discovery.must_ship_90_days))}</textarea></label><label class="field"><span>New concepts last month</span><input name="new_concepts_last_month" type="number" min="0" value="${discovery.new_concepts_last_month ?? ""}" /></label><label class="field"><span>Idea → usable asset (days)</span><input name="idea_to_usable_asset_days" type="number" min="0" step="0.1" value="${discovery.idea_to_usable_asset_days ?? ""}" /></label><label class="field"><span>Paid-media structure</span><select name="paid_media_structure">${optionList(["unknown", "consolidated_broad", "segmented", "mixed"], discovery.paid_media_structure || "unknown")}</select></label><label class="field"><span>Optimization event</span><input name="optimization_event" value="${escapeHtml(discovery.optimization_event || "")}" placeholder="Purchase, qualified lead…" /></label><label class="field"><span>Optimized events / week</span><input name="weekly_optimization_events" type="number" min="0" value="${discovery.weekly_optimization_events ?? ""}" /></label><label class="field"><span>Creative as constraint</span><select name="creative_constraint">${optionList(["unknown", "not_primary", "contributing", "primary"], discovery.creative_constraint || "unknown")}</select></label><label class="field form-wide"><span>Paid-media context <small>human-confirmed; never inferred from Ad Library</small></span><textarea name="paid_media_notes">${escapeHtml(discovery.paid_media_notes || "")}</textarea></label><label class="field"><span>Employee monthly cost</span><input name="employee_monthly_cost" type="number" min="0" step="0.01" value="${discovery.employee_monthly_cost ?? ""}" /></label><label class="field"><span>Agency monthly cost</span><input name="agency_monthly_cost" type="number" min="0" step="0.01" value="${discovery.agency_monthly_cost ?? ""}" /></label><label class="field"><span>Freelancer monthly cost</span><input name="freelancer_monthly_cost" type="number" min="0" step="0.01" value="${discovery.freelancer_monthly_cost ?? ""}" /></label><label class="field"><span>Vendor coordination hours</span><input name="vendor_coordination_hours" type="number" min="0" step="0.1" value="${discovery.vendor_coordination_hours ?? ""}" /></label><label class="field form-wide"><span>Fidelity / rights constraints</span><textarea name="fidelity_rights_constraints">${escapeHtml(linesFromArray(discovery.fidelity_rights_constraints))}</textarea></label><label class="field"><span>Approver name</span><input name="approver_name" value="${escapeHtml(approver.name || "")}" /></label><label class="field"><span>Approver role</span><input name="approver_role" value="${escapeHtml(approver.role || "")}" /></label><label class="field"><span>Approver turnaround (days)</span><input name="approver_turnaround_days" type="number" min="0" step="0.1" value="${discovery.approver_turnaround_days ?? ""}" /></label>${[0, 1, 2].map(index => `<label class="field"><span>Confirmed ask ${index + 1}</span><input name="discovery_ask_${index}" value="${escapeHtml(discovery.first_three_asks?.[index] || "")}" /></label>`).join("")}<label class="field form-wide"><span>Discovery notes</span><textarea name="notes">${escapeHtml(discovery.notes || "")}</textarea></label></div><div class="form-actions"><button class="button primary" type="submit">Save discovery</button></div></fieldset></form>
        <form id="outcome-form" class="workspace-form"><fieldset><legend>Sales outcome</legend><div class="form-grid"><label class="field"><span>Outreach outcome</span><select name="outreach_outcome">${optionList(["not_contacted", "contacted", "replied", "meeting_booked", "no_response"], outcome.outreach_outcome)}</select></label><label class="field"><span>Proposal outcome</span><select name="proposal_outcome">${optionList(["not_sent", "pending", "won", "lost"], outcome.proposal_outcome)}</select></label><label class="field"><span>Proposal sent at</span><input name="proposal_sent_at" type="datetime-local" value="${localDateTimeValue(outcome.proposal_sent_at)}" /></label><label class="field"><span>Monthly value</span><input name="proposal_monthly_value" type="number" min="0" step="0.01" value="${outcome.proposal_monthly_value ?? 5000}" /></label><label class="field"><span>Won at</span><input name="won_at" type="datetime-local" value="${localDateTimeValue(outcome.won_at)}" /></label><label class="field"><span>Lost at</span><input name="lost_at" type="datetime-local" value="${localDateTimeValue(outcome.lost_at)}" /></label><label class="field"><span>Month one started</span><input name="month_one_started_at" type="date" value="${dateInput(outcome.month_one_started_at)}" /></label><label class="field form-wide"><span>Lost reason <small>required when lost</small></span><input name="lost_reason" value="${escapeHtml(outcome.lost_reason || "")}" /></label><label class="field form-wide"><span>Outcome notes</span><textarea name="notes">${escapeHtml(outcome.notes || "")}</textarea></label></div><div class="form-actions"><button class="button primary" type="submit">Save outcome</button></div></fieldset></form>
      </section>

      <section class="workspace-section" id="account-delivery"><div class="workspace-section-head"><h3>Delivery & renewal</h3><div class="delivery-summary"><span>Delivered → used ${delivery.assets_delivered ? percentage(Number(delivery.assets_used || 0) / Number(delivery.assets_delivered)) : "—"}</span><span>Margin ${money(delivery.contribution_margin)}</span></div></div>
        <form id="delivery-form" class="workspace-form form-grid"><label class="field"><span>Specialist match (hours)</span><input name="time_to_specialist_match_hours" type="number" min="0" step="0.1" value="${delivery.time_to_specialist_match_hours ?? ""}" /></label><label class="field"><span>First useful draft (hours)</span><input name="time_to_first_useful_draft_hours" type="number" min="0" step="0.1" value="${delivery.time_to_first_useful_draft_hours ?? ""}" /></label><label class="field"><span>First activated asset (hours)</span><input name="time_to_first_activated_asset_hours" type="number" min="0" step="0.1" value="${delivery.time_to_first_activated_asset_hours ?? ""}" /></label><label class="field"><span>Cycle-time reduction %</span><input name="cycle_time_reduction_percent" type="number" min="0" max="100" step="0.1" value="${delivery.cycle_time_reduction_percent ?? ""}" /></label><label class="field"><span>Assets delivered</span><input name="assets_delivered" type="number" min="0" value="${delivery.assets_delivered || 0}" /></label><label class="field"><span>Assets used</span><input name="assets_used" type="number" min="0" value="${delivery.assets_used || 0}" /></label><label class="field"><span>Revision rounds</span><input name="revision_rounds" type="number" min="0" value="${delivery.revision_rounds || 0}" /></label><label class="field"><span>Revision hours</span><input name="revision_hours" type="number" min="0" step="0.1" value="${delivery.revision_hours || 0}" /></label><label class="field"><span>Internal hours displaced</span><input name="internal_hours_displaced" type="number" min="0" step="0.1" value="${delivery.internal_hours_displaced || 0}" /></label><label class="field"><span>Vendor coordination displaced</span><input name="vendor_coordination_hours_displaced" type="number" min="0" step="0.1" value="${delivery.vendor_coordination_hours_displaced || 0}" /></label><label class="field"><span>Month-two queue depth</span><input name="month_two_queue_depth" type="number" min="0" value="${delivery.month_two_queue_depth || 0}" /></label><label class="field"><span>Direct delivery cost</span><input name="direct_delivery_cost" type="number" min="0" step="0.01" value="${delivery.direct_delivery_cost ?? ""}" /></label><label class="field"><span>Recognized revenue</span><input name="recognized_revenue" type="number" min="0" step="0.01" value="${delivery.recognized_revenue ?? ""}" /></label><label class="field"><span>Delivery burden</span><select name="delivery_burden">${optionList(["unknown", "low", "medium", "high", "uneconomic"], delivery.delivery_burden)}</select></label><label class="field"><span>Day-21 renewal intent</span><select name="day_21_renewal_intent">${optionList(["unknown", "yes", "maybe", "no"], delivery.day_21_renewal_intent)}</select></label><label class="field"><span>Renewal decision</span><select name="renewal_decision">${optionList(["pending", "renewed", "not_renewed"], delivery.renewal_decision)}</select></label><label class="field"><span>Decision date</span><input name="renewal_decided_at" type="date" value="${dateInput(delivery.renewal_decided_at)}" /></label><label class="field form-wide"><span>Delivery / renewal notes</span><textarea name="notes">${escapeHtml(delivery.notes || "")}</textarea></label><div class="form-actions form-wide"><button class="button primary" type="submit">Save delivery & renewal</button></div></form>
      </section>

      <section class="workspace-section" id="account-evidence"><div class="workspace-section-head"><h3>Evidence</h3></div>
        <div class="evidence-layout"><form id="claim-form" class="workspace-form claim-form"><label class="field"><span>Claim</span><textarea name="claim" required rows="4"></textarea></label><div class="claim-coverage"><fieldset><legend>Principal signals covered</legend>${PRINCIPAL_SIGNALS.map(name => `<label class="check-field"><input type="checkbox" name="claim_signal" value="${name}" /><span>${escapeHtml(humanize(name))}</span></label>`).join("")}</fieldset><fieldset><legend>Hard gates covered</legend>${QUALIFICATION_GATES.map(name => `<label class="check-field"><input type="checkbox" name="claim_gate" value="${name}" /><span>${escapeHtml(humanize(name))}</span></label>`).join("")}</fieldset></div><div class="form-grid"><label class="field"><span>Epistemic status</span><select name="epistemic_status">${optionList(["observed", "inferred", "unknown"], "observed")}</select></label><label class="field"><span>Truth state</span><select name="truth_state">${optionList(TRUTH_STATES, "met")}</select></label><label class="field"><span>Source kind</span><select name="source_kind">${optionList(["company_site", "ad_library", "social_feed", "career_page", "press_release", "public_directory", "discovery_call", "internal_history", "warm_introduction", "operator_observation", "other"], "company_site")}</select></label><label class="field"><span>Source URL</span><input name="source_url" type="url" /></label><label class="field"><span>Observed at</span><input name="observed_at" type="datetime-local" required value="${localDateTimeValue(new Date().toISOString())}" /></label><label class="field"><span>Confidence</span><select name="confidence">${optionList(["low", "medium", "high"], "medium")}</select></label><label class="check-field"><input name="human_verified" type="checkbox" /><span>Human verified</span></label><label class="field"><span>Verified by</span><input name="verified_by" /></label></div><div class="form-actions"><button class="button primary" type="submit">Add claim</button></div></form>
        <div class="claim-list">${claims.length ? claims.map(claim => `<article class="claim-card"><header><div>${epistemicBadge(claim.epistemic_status, claim.human_verified)}${stateBadge(claim.truth_state)}</div><div class="claim-actions">${claim.human_verified ? "" : `<button class="text-button" data-verify-claim="${escapeHtml(claim.id)}">Verify</button>`}<button class="icon-button" data-delete-claim="${escapeHtml(claim.id)}" aria-label="Delete evidence claim">×</button></div></header><p>${escapeHtml(claim.claim)}</p><div class="claim-meta"><span>${escapeHtml(humanize(claim.source_kind))}</span><span>${escapeHtml(accountTimestamp(claim.observed_at))}</span><span>${escapeHtml(humanize(claim.confidence))} confidence</span></div><div class="claim-tags">${[...(claim.signals || []), ...(claim.gates || []), ...(claim.dimensions || [])].map(value => `<span>${escapeHtml(humanize(value))}</span>`).join("") || `<span>Unlinked</span>`}</div>${claim.source_url ? `<a href="${safeUrl(claim.source_url)}" target="_blank" rel="noopener">Source ↗</a>` : ""}</article>`).join("") : `<p class="empty">No claims.</p>`}</div></div>
      </section>

      <section class="workspace-section" id="account-activity"><div class="workspace-section-head"><h3>Activity</h3></div><div class="timeline account-timeline">${activities.length ? activities.map(event => `<div class="timeline-item"><strong>${escapeHtml(humanize(event.activity_type || event.type))}</strong><span>${escapeHtml(event.summary || "Updated")} · ${escapeHtml(accountTimestamp(event.occurred_at))}</span></div>`).join("") : `<p class="empty">No activity.</p>`}</div></section>
    </div>`;
  compactDealWorkspace();
  bindAccountWorkspace(account);
  enhanceMultiChecklists($("#account-workspace-content"));
}

// Keep outcome entry primary while preserving all saved research and editing tools.
function compactDealWorkspace() {
  const body = $("#account-workspace .account-workspace-body");
  const outcome = $("#outcome-form");
  const primary = document.createElement("section");
  primary.className = "workspace-section";
  primary.append(outcome);
  const advanced = document.createElement("details");
  advanced.className = "workspace-section deal-more-details";
  const summary = document.createElement("summary");
  summary.textContent = "Company details & history";
  advanced.append(summary);
  $("#account-workspace .workspace-nav")?.remove();
  for (const section of [...body.children]) {
    const details = document.createElement("details");
    const label = document.createElement("summary");
    label.textContent = section.querySelector("h3")?.textContent || "Research";
    details.append(label, section);
    advanced.append(details);
  }
  body.replaceChildren(primary, advanced);
}

function revealDealSection(id) {
  const section = document.getElementById(id);
  for (let parent = section?.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
  section?.scrollIntoView({behavior:"smooth"});
}

async function openAccount(accountId, { focusActivity = false } = {}) {
  closeDrawer();
  const workspace = $("#account-workspace");
  workspace.classList.add("open");
  workspace.setAttribute("aria-hidden", "false");
  $("#account-backdrop").hidden = false;
  $("#account-workspace-content").innerHTML = `<div class="drawer-loading">${loadingIndicator("Loading account…")}</div>`;
  try {
    renderAccountWorkspace(await api(`/api/accounts/${encodeURIComponent(accountId)}`));
    $("#close-account-workspace")?.focus();
    if (focusActivity) $("#outcome-form")?.scrollIntoView({block:"start"});
  } catch (error) {
    $("#account-workspace-content").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

function closeAccountWorkspace() {
  $("#account-workspace").classList.remove("open");
  $("#account-workspace").setAttribute("aria-hidden", "true");
  $("#account-backdrop").hidden = true;
  state.activeAccount = null;
}

async function refreshAccountViews() {
  await Promise.all([
    state.activeView === "overview" ? loadDashboard() : Promise.resolve(),
    state.activeView === "accounts" ? loadAccounts() : Promise.resolve(),
    state.activeView === "sales" ? loadSalesWorkbench({ invalidate: true }) : Promise.resolve(),
  ]);
}

async function saveAccountDocument(path, body, message) {
  const account = state.activeAccount;
  if (!account) return;
  const refreshed = unwrapAccount(await api(`/api/accounts/${encodeURIComponent(account.id)}/${path}`, { method: "PUT", body: JSON.stringify(body) }));
  showToast(message);
  renderAccountWorkspace(refreshed);
  await refreshAccountViews();
}

function bindAccountWorkspace(account) {
  $$("#account-workspace .workspace-nav a").forEach(anchor => {
    anchor.addEventListener("click", event => {
      event.preventDefault();
      document.getElementById(anchor.getAttribute("href").slice(1))?.scrollIntoView({behavior:"smooth"});
    });
  });
  $("#close-account-workspace").addEventListener("click", closeAccountWorkspace);
  $("#account-review-evidence").addEventListener("click", () => revealDealSection("account-evidence"));
  $("#account-profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = Object.fromEntries(new FormData(form));
    if (body.segment === "agency_buyer") body.relationship_type = "buyer";
    if (body.segment === "agency_referral_partner") body.relationship_type = "referral_partner";
    try {
      const refreshed = unwrapAccount(await api(`/api/accounts/${encodeURIComponent(account.id)}`, { method: "PATCH", body: JSON.stringify(body) }));
      showToast("Account profile saved");
      renderAccountWorkspace(refreshed);
      await refreshAccountViews();
    } catch (error) { showToast(error.message, "error"); }
  });
  $("#qualification-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const body = {
      ...(account.qualification || {}),
      version: "creative-capacity-v1",
      dimensions: Object.fromEntries(Object.keys(DIMENSION_LIMITS).map(name => [name, Number(form.elements[name].value || 0)])),
      penalties: [0, 1, 2, 3, 4].map(index => ({ name: form.elements[`penalty_name_${index}`].value.trim(), points: Number(form.elements[`penalty_points_${index}`].value || 0), reason: form.elements[`penalty_reason_${index}`].value.trim() })).filter(penalty => penalty.name && penalty.reason && penalty.points > 0),
      principal_signals: Object.fromEntries(PRINCIPAL_SIGNALS.map(name => [name, form.elements[name].value])),
      gates: Object.fromEntries(QUALIFICATION_GATES.map(name => [name, form.elements[name].value])),
      confidence: form.elements.confidence.value,
      buyer: form.elements.buyer_role.value.trim() ? { name: form.elements.buyer_name.value.trim(), role: form.elements.buyer_role.value.trim() } : null,
      why_now: form.elements.why_now.value,
      first_month_asks: [0, 1, 2].map(index => form.elements[`ask_${index}`].value),
    };
    try { await saveAccountDocument("qualification", body, "Qualification recomputed"); } catch (error) { showToast(error.message, "error"); }
  });
  $("#capacity-map-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const body = {
      version: "creative-capacity-map-v1", current_promotion: arrayFromLines(form.elements.current_promotion.value), distinct_concept_count: numberOrNull(form.elements.distinct_concept_count.value), concept_notes: arrayFromLines(form.elements.concept_notes.value), observed_demand: arrayFromLines(form.elements.observed_demand.value), production_gap_hypothesis: form.elements.production_gap_hypothesis.value,
      first_month_asks: [0, 1, 2].map(index => form.elements[`map_ask_${index}`].value), first_month_queue: arrayFromLines(form.elements.first_month_queue.value), economic_buyer: form.elements.map_buyer_role.value.trim() ? { name: form.elements.map_buyer_name.value.trim(), role: form.elements.map_buyer_role.value.trim() } : null, rights_ai_objection: form.elements.rights_ai_objection.value,
    };
    try { await saveAccountDocument("capacity-map", body, "Creative Capacity Map saved"); } catch (error) { showToast(error.message, "error"); }
  });
  $("#discovery-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const firstThreeAsks = [0, 1, 2].map(index => form.elements[`discovery_ask_${index}`].value.trim()).filter(Boolean);
    if (form.elements.completed.checked && !form.elements.held_at.value) {
      showToast("Completed discovery requires a held date", "error");
      return;
    }
    const body = { outcome: form.elements.discovery_outcome.value, outcome_reason: form.elements.outcome_reason.value, research_minutes: Number(form.elements.research_minutes.value || 0), confirmed_recurring_need: form.elements.confirmed_recurring_need.value === "" ? null : form.elements.confirmed_recurring_need.value === "true", held_at: isoOrNull(form.elements.held_at.value), creative_backlog: arrayFromLines(form.elements.creative_backlog.value), must_ship_30_days: arrayFromLines(form.elements.must_ship_30_days.value), must_ship_60_days: arrayFromLines(form.elements.must_ship_60_days.value), must_ship_90_days: arrayFromLines(form.elements.must_ship_90_days.value), new_concepts_last_month: numberOrNull(form.elements.new_concepts_last_month.value), idea_to_usable_asset_days: numberOrNull(form.elements.idea_to_usable_asset_days.value), paid_media_structure: form.elements.paid_media_structure.value, optimization_event: form.elements.optimization_event.value.trim(), weekly_optimization_events: numberOrNull(form.elements.weekly_optimization_events.value), creative_constraint: form.elements.creative_constraint.value, paid_media_notes: form.elements.paid_media_notes.value, employee_monthly_cost: numberOrNull(form.elements.employee_monthly_cost.value), agency_monthly_cost: numberOrNull(form.elements.agency_monthly_cost.value), freelancer_monthly_cost: numberOrNull(form.elements.freelancer_monthly_cost.value), vendor_coordination_hours: numberOrNull(form.elements.vendor_coordination_hours.value), fidelity_rights_constraints: arrayFromLines(form.elements.fidelity_rights_constraints.value), approver: form.elements.approver_role.value.trim() ? { name: form.elements.approver_name.value.trim(), role: form.elements.approver_role.value.trim() } : null, approver_turnaround_days: numberOrNull(form.elements.approver_turnaround_days.value), first_three_asks: firstThreeAsks, notes: form.elements.notes.value, completed: form.elements.completed.checked };
    try { await saveAccountDocument("discovery", body, "Scan review saved"); } catch (error) { showToast(error.message, "error"); }
  });
  $("#outcome-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const body = { outreach_outcome: form.elements.outreach_outcome.value, proposal_outcome: form.elements.proposal_outcome.value, proposal_sent_at: isoOrNull(form.elements.proposal_sent_at.value), proposal_monthly_value: numberOrNull(form.elements.proposal_monthly_value.value), won_at: isoOrNull(form.elements.won_at.value), lost_at: isoOrNull(form.elements.lost_at.value), lost_reason: form.elements.lost_reason.value, month_one_started_at: form.elements.month_one_started_at.value || null, notes: form.elements.notes.value };
    try { await saveAccountDocument("outcome", body, "Sales outcome saved"); } catch (error) { showToast(error.message, "error"); }
  });
  $("#delivery-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    const numericFields = ["time_to_specialist_match_hours", "time_to_first_useful_draft_hours", "time_to_first_activated_asset_hours", "cycle_time_reduction_percent", "direct_delivery_cost", "recognized_revenue"];
    const zeroFields = ["assets_delivered", "assets_used", "revision_rounds", "revision_hours", "internal_hours_displaced", "vendor_coordination_hours_displaced", "month_two_queue_depth"];
    const body = Object.fromEntries(numericFields.map(name => [name, numberOrNull(form.elements[name].value)]));
    zeroFields.forEach(name => { body[name] = Number(form.elements[name].value || 0); });
    Object.assign(body, { delivery_burden: form.elements.delivery_burden.value, day_21_renewal_intent: form.elements.day_21_renewal_intent.value, renewal_decision: form.elements.renewal_decision.value, renewal_decided_at: form.elements.renewal_decided_at.value || null, notes: form.elements.notes.value });
    try { await saveAccountDocument("delivery", body, "Delivery and renewal saved"); } catch (error) { showToast(error.message, "error"); }
  });
  const verificationToggle = $("#claim-form").elements.human_verified;
  const verifierInput = $("#claim-form").elements.verified_by;
  verificationToggle.addEventListener("change", () => {
    verifierInput.required = verificationToggle.checked;
    verifierInput.setAttribute("aria-required", String(verificationToggle.checked));
  });
  $("#claim-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const verified = form.elements.human_verified.checked;
    const body = { signals: $$('input[name="claim_signal"]:checked', form).map(input => input.value), gates: $$('input[name="claim_gate"]:checked', form).map(input => input.value), dimensions: [], claim: form.elements.claim.value, epistemic_status: form.elements.epistemic_status.value, truth_state: form.elements.truth_state.value, source_kind: form.elements.source_kind.value, source_url: form.elements.source_url.value || null, observed_at: isoOrNull(form.elements.observed_at.value), confidence: form.elements.confidence.value, human_verified: verified, verified_by: verified ? form.elements.verified_by.value.trim() : "", verified_at: verified ? new Date().toISOString() : null };
    try {
      const refreshed = unwrapAccount(await api(`/api/accounts/${encodeURIComponent(account.id)}/claims`, { method: "POST", body: JSON.stringify(body) }));
      showToast("Evidence claim added"); renderAccountWorkspace(refreshed); await refreshAccountViews();
    } catch (error) { showToast(error.message, "error"); }
  });
}

function renderTargetViewCoverage(status = {}, shortfall = null) {
  const discovered = statusNumber(status, ["candidate_inventory", "inventory_candidates", "total_candidates", "candidate_count", "discovered_candidates", "inventory.candidates"], 0);
  const inspected = statusNumber(status, ["inspected_candidates", "inspected", "inventory.inspected"], 0);
  const hydrated = statusNumber(status, ["hydrated_latest_candidates", "hydrated_candidates", "hydrated_latest", "inventory.hydrated"], 0);
  const uninspected = statusNumber(status, ["uninspected_candidates", "uninspected", "backlog", "inventory.uninspected"], Math.max(0, discovered - inspected));
  const coverageRaw = firstValue(status, ["coverage", "coverage_ratio", "projection_coverage"], discovered ? hydrated / discovered : 0);
  const coverage = Number(coverageRaw || 0);
  $("#target-view-title").textContent = `${coverage <= 1 ? (coverage * 100).toFixed(1) : coverage.toFixed(1)}% hydrated`;
  $("#target-view-copy").textContent = `${formatNumber(hydrated)} projected · ${formatNumber(inspected)} inspected`;
  $("#target-view-backlog").textContent = `${formatNumber(uninspected)} uninspected`;
  $("#target-view-shortfall").textContent = shortfallCopy(shortfall, status);
}

function prospectsShell() {
  const filters = state.filters;
  $("#view-prospects").innerHTML = `
    <section class="target-view-status page-body" aria-live="polite"><div><span class="coverage-pulse" aria-hidden="true"></span><div><strong id="target-view-title">Loading…</strong><small id="target-view-copy"></small></div></div><span id="target-view-backlog">— uninspected</span></section>
    <div class="target-shortfall" id="target-view-shortfall">Loading…</div>
    <div class="toolbar target-toolbar">
      <label class="toolbar-label"><span>Lane</span><select id="target-lane" aria-label="Target lane">${optionList(["buyer", "referral_partner", "all"], filters.lane)}</select></label>
      <label class="toolbar-label"><span>Min score</span><input class="toolbar-input score-filter" id="target-min-score" type="number" min="0" max="100" value="${escapeHtml(filters.min_score)}" placeholder="Any" /></label>
      <label class="toolbar-label"><span>Archetype</span><select id="target-archetype" aria-label="Target archetype"><option value="" ${filters.archetype === "" ? "selected" : ""}>All</option><option value="agency" ${filters.archetype === "agency" ? "selected" : ""}>Agency</option><option value="ecommerce_brand" ${filters.archetype === "ecommerce_brand" ? "selected" : ""}>Ecommerce brand</option><option value="saas_or_other" ${filters.archetype === "saas_or_other" ? "selected" : ""}>B2B / SaaS</option><option value="creator_education" ${filters.archetype === "creator_education" ? "selected" : ""}>Education</option></select></label>
      <label class="toolbar-label"><span>Order</span><select id="target-sort" aria-label="Target order"><option value="priority" ${filters.sort === "priority" ? "selected" : ""}>Priority</option><option value="durable_fit" ${filters.sort === "durable_fit" ? "selected" : ""}>Fit</option><option value="contact_now" ${filters.sort === "contact_now" ? "selected" : ""}>Contact now</option><option value="target_score" ${filters.sort === "target_score" ? "selected" : ""}>Raw target score</option><option value="failure_rate" ${filters.sort === "failure_rate" ? "selected" : ""}>Failure rate</option></select></label>
      <span class="result-count" id="prospect-count">Loading…</span>
    </div>
    <section class="panel"><div class="table-wrap"><table class="prospect-table target-table">
      <thead><tr><th>Company</th><th><button class="sort-header" data-target-sort="durable_fit" type="button">Fit</button></th><th><button class="sort-header" data-target-sort="contact_now" type="button">Now</button></th><th><button class="sort-header" data-target-sort="priority" type="button">Priority</button></th><th>Action</th><th><button class="sort-header" data-target-sort="failure_rate" type="button">Fails</button></th><th>Why now</th><th>Contact</th></tr></thead>
      <tbody id="prospect-body"><tr><td colspan="8" class="loading-row">${loadingIndicator("Loading targets…")}</td></tr></tbody>
    </table></div><div class="pagination"><span id="prospect-range">—</span><div class="pagination-actions"><button class="button" id="prospect-previous" type="button">Previous</button><button class="button" id="prospect-next" type="button">Next</button></div></div></section>`;
  [["#target-lane", "lane"], ["#target-min-score", "min_score"], ["#target-archetype", "archetype"], ["#target-sort", "sort"]].forEach(([selector, key]) => {
    $(selector).addEventListener("change", event => { state.filters[key] = event.target.value; state.prospectOffset = 0; loadProspects(); });
  });
  $("#prospect-previous").addEventListener("click", () => { state.prospectOffset = Math.max(0, state.prospectOffset - RESULT_PAGE_SIZE); loadProspects(); });
  $("#prospect-next").addEventListener("click", () => { state.prospectOffset += RESULT_PAGE_SIZE; loadProspects(); });
  syncTargetSortHeaders($("#view-prospects"), filters.sort);
}

async function loadProspects() {
  if (!$("#prospect-body")) return;
  const requestId = ++state.prospectRequestId;
  $("#prospect-body").innerHTML = `<tr><td colspan="8" class="loading-row">${loadingIndicator("Loading targets…")}</td></tr>`;
  const params = prospectParams();
  try {
    const result = await api(`/api/targets?${params}`);
    if (requestId !== state.prospectRequestId) return;
    state.prospects = result.items || [];
    state.prospectTotal = Number(result.total || 0);
    if (!state.prospects.length && state.prospectOffset > 0 && state.prospectTotal > 0) {
      state.prospectOffset = Math.floor((state.prospectTotal - 1) / RESULT_PAGE_SIZE) * RESULT_PAGE_SIZE;
      return loadProspects();
    }
    state.targetStatus = result.status || state.targetStatus || {};
    state.targetShortfall = result.shortfall ?? state.targetShortfall;
    $("#prospect-body").innerHTML = prospectRows(state.prospects);
    syncTargetSortHeaders($("#view-prospects"), state.filters.sort);
    $("#prospect-count").textContent = `${formatNumber(state.prospectTotal)} targets`;
    setPageSearchStatus(state.pageSearch.trim() ? `${formatNumber(state.prospectTotal)} matches` : "");
    const first = state.prospectOffset + 1;
    const last = state.prospectOffset + state.prospects.length;
    $("#prospect-range").textContent = state.prospects.length ? `Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(state.prospectTotal)}` : "No targets";
    $("#prospect-previous").disabled = state.prospectOffset === 0;
    $("#prospect-next").disabled = last >= state.prospectTotal;
    renderTargetViewCoverage(state.targetStatus, state.targetShortfall);
  } catch (error) {
    if (requestId !== state.prospectRequestId) return;
    showToast(error.message, "error");
    $("#prospect-body").innerHTML = `<tr><td colspan="8" class="loading-row">Could not load targets.</td></tr>`;
    $("#target-view-title").textContent = "Target intelligence unavailable";
    $("#target-view-copy").textContent = error.message;
  }
}

function sourcesShell() {
  $("#view-sources").innerHTML = `
    <section class="panel source-discovery-panel" aria-labelledby="source-discovery-title">
      <div class="source-discovery-head">
        <h3 id="source-discovery-title">Find more public sources</h3>
        <div class="sources-actions"><button class="button" id="add-source" type="button">Add source</button><button class="button primary" id="find-more-sources" data-source-scout-start="defaults" type="button" disabled>Find More</button></div>
      </div>
      <div class="scout-message" id="sources-scout-message" data-scout-message aria-live="polite" hidden></div>
      <div class="source-review-head">
        <h3>Source ideas</h3>
        <div class="intelligence-stats" id="sources-intelligence-stats" data-scout-stats></div>
      </div>
      <div class="table-wrap source-suggestion-table"><table class="source-table">
        <thead><tr><th>Source</th><th>Kind</th><th>Score</th><th>Mechanism</th><th>Evidence</th><th>Status</th><th></th></tr></thead>
        <tbody id="sources-suggestion-body" data-suggestion-body><tr><td colspan="7" class="loading-row">${loadingIndicator()}</td></tr></tbody>
      </table></div>
    </section>
    <div class="source-inventory-head"><h3 id="active-discovery-source-title">Typed source activation queue</h3><span class="badge" id="active-discovery-source-count">Loading…</span></div>
    <section class="panel table-wrap source-inventory-panel" aria-labelledby="active-discovery-source-title"><table class="source-table active-discovery-table">
      <thead><tr><th>Source</th><th>Method</th><th>Status</th><th>Found</th><th>Hydrated</th><th>Blockers</th><th></th></tr></thead>
      <tbody id="active-discovery-source-body"><tr><td colspan="7" class="loading-row">${loadingIndicator("Loading activation queue…")}</td></tr></tbody>
    </table></section>
    <div class="source-registry-head"><h3>Generic Web &amp; CT apex registry</h3><p>Web &amp; CT status controls whether each apex participates in generic website scanning.</p></div>
    <div class="source-groups" id="source-groups"><div class="panel"><p class="empty">${loadingIndicator("Loading registry…")}</p></div></div>
    <div class="source-inventory-head accepted-source-head"><div><h3 id="accepted-source-title">Accepted scan sources</h3><p>Typed sources scan through their matching scan method, not the generic Web &amp; CT scanner.</p></div><span class="badge" id="accepted-source-count">Loading…</span></div>
    <section class="panel table-wrap accepted-source-panel" aria-labelledby="accepted-source-title"><table class="source-table accepted-source-table">
      <thead><tr><th>Source</th><th>Method</th><th>Status</th><th>Found</th><th>Hydrated</th><th>Last scan</th><th></th></tr></thead>
      <tbody id="accepted-source-body"><tr><td colspan="7" class="loading-row">${loadingIndicator("Loading accepted sources…")}</td></tr></tbody>
    </table></section>`;
  $("#add-source").addEventListener("click", () => $("#source-modal").showModal());
  $("#find-more-sources").addEventListener("click", startSourceScout);
}

function discoverySourceBlockers(item = {}) {
  const value = item.blockers ?? item.activation?.blockers;
  const blockers = Array.isArray(value) ? value : (value ? [value] : []);
  return blockers.map(blocker => typeof blocker === "string"
    ? blocker
    : blocker?.message || blocker?.detail || blocker?.reason || JSON.stringify(blocker));
}

function discoveryTabForSourceItem(item = {}) {
  const method = item.method || item.execution_method || "";
  const tabId = canonicalDiscoveryTabId(method, item.execution_method_label || item.method_label || "");
  return discoveryTabs().find(tab => tab.id === tabId) || discoveryTabForSourceKind(item.source_kind);
}

function discoverySourceRun(item = {}) {
  return item.latest_run && typeof item.latest_run === "object" ? item.latest_run : null;
}

function discoverySourceActivationState(item = {}) {
  const blockers = discoverySourceBlockers(item);
  if (blockers.length) return "blocked";
  const latestRun = discoverySourceRun(item);
  return String(item.activation_state || item.activation?.state || item.execution_state || latestRun?.status || "active").toLowerCase();
}

function discoverySourceRunsActive(items = state.discoveryItems) {
  return items.some(item => ACTIVE_RUN_STATES.has(discoverySourceActivationState(item)));
}

function sourceActivationToast(activation = {}, suggestion = {}) {
  const activationState = String(activation.state || "accepted").toLowerCase().replaceAll("-", "_");
  const tab = discoveryTabForSourceItem({ ...suggestion, method: activation.method || suggestion.method });
  const methodLabel = tab?.label || humanize(activation.method || suggestion.execution_method || suggestion.source_kind || "discovery");
  const blockers = Array.isArray(activation.blockers) ? activation.blockers : (activation.blockers ? [activation.blockers] : []);
  const blocker = blockers[0];
  const blockerMessage = typeof blocker === "string" ? blocker : blocker?.message || blocker?.detail || blocker?.reason;
  if (activationState === "queued") return `Source accepted · queued in ${methodLabel}`;
  if (["starting", "running", "retrying"].includes(activationState)) return `Source accepted · scanning in ${methodLabel}`;
  if (activationState === "blocked") return `Source accepted · activation blocked${blockerMessage ? `: ${blockerMessage}` : activation.message ? `: ${activation.message}` : ""}`;
  if (["already_active", "active"].includes(activationState)) return `Source accepted · already active in ${methodLabel}`;
  return activation.message || `Source accepted · available in ${methodLabel}`;
}

function renderDiscoverySourceInventory() {
  const root = $("#active-discovery-source-body");
  const count = $("#active-discovery-source-count");
  if (!root || !count) return;
  const items = (Array.isArray(state.discoveryItems) ? state.discoveryItems : [])
    .filter(item => item.queue_visible !== false
      && !state.pendingSourceDismissals.has(String(item.id))
      && !["complete", "completed"].includes(discoverySourceActivationState(item)));
  count.textContent = `${formatNumber(items.length)} in queue`;
  count.className = `badge ${items.length ? "moss" : ""}`;
  root.innerHTML = items.length ? items.map(item => {
    const tab = discoveryTabForSourceItem(item);
    const latestRun = discoverySourceRun(item);
    const status = discoverySourceActivationState(item);
    const blockers = discoverySourceBlockers(item);
    const candidatesFound = Number(firstValue(item, ["candidates_found", "latest_run.candidates_found", "latest_run.candidates_persisted", "latest_run.summary.candidates_found", "latest_run.summary.candidates_persisted"], 0));
    const candidatesHydrated = Number(firstValue(item, ["candidates_hydrated", "latest_run.candidates_hydrated", "latest_run.summary.candidates_hydrated"], 0));
    const hydrationRemaining = Number(item.hydration_remaining ?? Math.max(0, candidatesFound - candidatesHydrated));
    const sourceExecutionState = String(item.source_execution_state || status);
    const sourceLabel = item.name || item.apex || item.source_key || item.url || "Scan source";
    const relationshipLabel = item.relationship_type === "referral_partner" ? "Referral" : "Buyer";
    const familyLabel = humanize(item.source_family || "other");
    const archetypeLabel = (item.buyer_archetypes || []).slice(0, 3).map(humanize).join(" · ");
    const duplicateCount = Number(item.duplicate_count || 0);
    const identityNote = duplicateCount ? `${formatNumber(duplicateCount)} historical alias${duplicateCount === 1 ? "" : "es"} consolidated${item.review_conflict ? " · review history differs" : ""}` : "";
    const sourceDetail = [
      item.apex || item.source_key || item.url || humanize(item.source_kind || "source"),
      relationshipLabel,
      familyLabel,
      archetypeLabel,
      identityNote,
    ].filter(Boolean).join(" · ");
    const rawSourceUrl = item.url || item.source_url || "";
    const invalidSourceUrl = blockers.some(blocker => blocker.startsWith("Source URL"));
    const sourceUrl = rawSourceUrl && !invalidSourceUrl ? safeUrl(rawSourceUrl) : "#";
    const statusClass = ["blocked", "failed", "partial", "hydration_due"].includes(status) ? "warn" : ["queued", "starting", "running", "retrying", "cancelling", "complete", "completed", "active", "already_active"].includes(status) ? "moss" : "";
    const runDetail = latestRun
      ? `${latestRun.id || latestRun.run_id ? `Scan ${latestRun.id || latestRun.run_id} · ` : ""}Source ${humanize(sourceExecutionState)}${hydrationRemaining ? ` · ${formatNumber(hydrationRemaining)} hydration due` : ""}`
      : blockers.length ? "Accepted · activation blocked" : "Accepted and available";
    const methodDetail = [item.execution_method ? humanize(item.execution_method) : "", runDetail].filter(Boolean).join(" · ");
    const hydrationAction = hydrationRemaining > 0
      ? `<button class="text-button" type="button" data-hydrate-source="${escapeHtml(item.id)}">Hydrate ${formatNumber(hydrationRemaining)} with AI</button>`
      : "";
    const retryable = ["failed", "partial", "skipped"].includes(status);
    const sourceAction = tab && status !== "hydration_due"
      ? `<button class="text-button source-open-method" type="button" data-open-method-tab="${escapeHtml(tab.id)}">${retryable ? "Retry source" : item.scan_eligible ? "Scan source" : "Open method"}</button>`
      : "";
    return `<tr class="source-inventory-row source-inventory-${escapeHtml(status)}">
      <td class="source-name"><strong>${sourceUrl === "#" ? escapeHtml(sourceLabel) : `<a href="${sourceUrl}" target="_blank" rel="noopener">${escapeHtml(sourceLabel)}</a>`}</strong><small>${escapeHtml(sourceDetail)}</small></td>
      <td><strong class="source-method-name">${escapeHtml(tab?.label || humanize(item.execution_method || item.source_kind || "discovery"))}</strong><small class="source-run-detail">${escapeHtml(methodDetail)}</small></td>
      <td><span class="badge ${statusClass}">${escapeHtml(humanize(status))}</span></td>
      <td class="source-count"><strong>${formatNumber(candidatesFound)}</strong></td>
      <td class="source-count"><strong>${formatNumber(candidatesHydrated)}</strong></td>
      <td class="source-blockers">${blockers.length ? `<ul>${blockers.slice(0, 2).map(blocker => `<li>${escapeHtml(blocker)}</li>`).join("")}</ul>` : `<span>None</span>`}</td>
      <td><div class="source-action-cell"><div class="source-row-actions">${hydrationAction}${sourceAction || (hydrationAction ? "" : "—")}</div><button class="icon-button source-dismiss-button" type="button" data-dismiss-source="${escapeHtml(item.id)}" aria-label="Dismiss ${escapeHtml(sourceLabel)} permanently">×</button></div></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="source-inventory-empty"><strong>All accepted sources processed</strong></td></tr>`;
}

function renderAcceptedSourceInventory() {
  const root = $("#accepted-source-body");
  const count = $("#accepted-source-count");
  if (!root || !count) return;
  const items = (Array.isArray(state.acceptedSources) ? state.acceptedSources : [])
    .filter(item => !state.pendingSourceDismissals.has(String(item.id)));
  count.textContent = `${formatNumber(items.length)} accepted`;
  count.className = `badge ${items.length ? "moss" : ""}`;
  root.innerHTML = items.length ? items.map(item => {
    const tab = discoveryTabForSourceItem(item);
    const latestRun = discoverySourceRun(item);
    const status = discoverySourceActivationState(item);
    const sourceLabel = item.name || item.apex || item.source_key || item.url || "Scan source";
    const rawSourceUrl = item.url || item.source_url || "";
    const sourceUrl = rawSourceUrl && !discoverySourceBlockers(item).some(blocker => blocker.startsWith("Source URL")) ? safeUrl(rawSourceUrl) : "#";
    const candidatesFound = Number(item.candidates_found || latestRun?.candidates_found || 0);
    const candidatesHydrated = Number(item.candidates_hydrated || latestRun?.candidates_hydrated || 0);
    const lastRunAt = latestRun?.finished_at || latestRun?.updated_at || latestRun?.started_at || item.last_candidate_at || item.last_seen_at;
    const statusClass = ["blocked", "failed", "partial"].includes(status) ? "warn" : ["queued", "starting", "running", "retrying", "complete", "completed", "active", "already_active"].includes(status) ? "moss" : "";
    return `<tr>
      <td class="source-name"><strong>${sourceUrl === "#" ? escapeHtml(sourceLabel) : `<a href="${sourceUrl}" target="_blank" rel="noopener">${escapeHtml(sourceLabel)}</a>`}</strong><small>${escapeHtml(item.apex || item.source_key || item.url || humanize(item.source_kind || "source"))}</small></td>
      <td><strong class="source-method-name">${escapeHtml(tab?.label || humanize(item.execution_method || item.source_kind || "discovery"))}</strong><small class="source-run-detail">${escapeHtml(humanize(item.source_kind || "typed source"))}</small></td>
      <td><span class="badge ${statusClass}">${escapeHtml(humanize(status))}</span></td>
      <td class="source-count"><strong>${formatNumber(candidatesFound)}</strong></td>
      <td class="source-count"><strong>${formatNumber(candidatesHydrated)}</strong></td>
      <td><span>${escapeHtml(lastRunAt ? relativeTime(lastRunAt) : "Never")}</span></td>
      <td>${tab ? `<button class="text-button source-open-method" type="button" data-open-method-tab="${escapeHtml(tab.id)}">Open method</button>` : "—"}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="source-inventory-empty"><strong>No accepted scan sources yet</strong><span>Accept a source idea to add it here.</span></td></tr>`;
}

function renderSources() {
  const grouped = state.sources.reduce((result, item) => { (result[item.category] ||= []).push(item); return result; }, {});
  renderDiscoverySourceInventory();
  renderAcceptedSourceInventory();
  $("#source-groups").innerHTML = Object.entries(grouped).map(([category, items]) => `
    <section class="source-group"><h3>${escapeHtml(category)} · ${items.length}</h3><div class="panel table-wrap"><table class="source-table">
      <thead><tr>${SOURCE_SORT_COLUMNS.map(sourceSortHeader).join("")}</tr></thead>
      <tbody>${sortedSourceItems(items).map(item => `<tr>
        <td class="source-name"><strong>${escapeHtml(item.apex)}</strong><small>Tier ${escapeHtml(item.tier)} · ${escapeHtml(item.category)}</small></td>
        <td><span class="source-rank">${Number(item.opportunity_score || 0)}</span></td>
        <td class="source-count">${formatNumber(item.inspected_count)}</td>
        <td class="source-count">${Number(item.live_rate || 0).toFixed(1)}%</td>
        <td class="source-count">${Number(item.high_priority_yield || 0).toFixed(1)}%</td>
        <td class="source-count">${Number(item.placement_ready_yield || 0).toFixed(1)}%</td>
        <td class="source-count">${Number(item.contactable_yield || 0).toFixed(1)}%</td>
        <td class="source-count source-failure-rate"><strong>${Number(item.scan_attempt_count || 0) ? `${Number(item.scan_failure_rate || 0).toFixed(1)}%` : "—"}</strong><small>${formatNumber(item.scan_failure_count)} / ${formatNumber(item.scan_attempt_count)}</small></td>
        <td class="source-count">${item.indexed_names == null ? "—" : formatNumber(Math.max(item.indexed_names - item.candidate_count, 0))}</td>
        <td><div class="source-web-ct-status"><label class="toggle"><input type="checkbox" data-source-toggle="${escapeHtml(item.apex)}" aria-label="Include ${escapeHtml(item.apex)} in Web and CT scans" ${item.enabled ? "checked" : ""} ${item.tier === "Public" ? "disabled" : ""} /><span></span></label><small>${item.enabled ? "Included" : "Not scanned here"}</small></div></td>
      </tr>`).join("")}</tbody></table></div></section>`).join("");
}

function sourceSortValue(item, key) {
  if (key === "backlog") return item.indexed_names == null ? null : Math.max(Number(item.indexed_names || 0) - Number(item.candidate_count || 0), 0);
  return item[key];
}

function sortedSourceItems(items) {
  const column = SOURCE_SORT_COLUMNS.find(value => value.key === state.sourceSort.key);
  if (!column) return items;
  const direction = state.sourceSort.direction === "desc" ? -1 : 1;
  return [...items].sort((left, right) => {
    const leftValue = sourceSortValue(left, column.key);
    const rightValue = sourceSortValue(right, column.key);
    const leftMissing = leftValue == null || (column.type === "number" && Number.isNaN(Number(leftValue)));
    const rightMissing = rightValue == null || (column.type === "number" && Number.isNaN(Number(rightValue)));
    if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
    if (leftMissing) return String(left.apex || "").localeCompare(String(right.apex || ""));
    let comparison;
    if (column.type === "text") comparison = String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: "base" });
    else if (column.type === "boolean") comparison = Number(Boolean(leftValue)) - Number(Boolean(rightValue));
    else comparison = Number(leftValue) - Number(rightValue);
    return comparison ? comparison * direction : String(left.apex || "").localeCompare(String(right.apex || ""));
  });
}

function sourceSortHeader(column) {
  const active = state.sourceSort.key === column.key;
  const direction = active ? state.sourceSort.direction : "none";
  const indicator = active ? (direction === "asc" ? "↑" : "↓") : "↕";
  return `<th aria-sort="${direction === "none" ? "none" : direction === "asc" ? "ascending" : "descending"}"><button class="source-sort-button" type="button" data-source-sort="${escapeHtml(column.key)}">${escapeHtml(column.label)}<span aria-hidden="true">${indicator}</span></button></th>`;
}

function applySourcesPayload(result) {
  state.sources = Array.isArray(result.items) ? result.items : [];
  state.discoveryItems = Array.isArray(result.discovery_items) ? result.discovery_items : [];
  state.acceptedSources = Array.isArray(result.accepted_items) ? result.accepted_items : state.discoveryItems;
  const inventoryRuns = state.discoveryItems.map(discoverySourceRun).filter(run => run && ACTIVE_RUN_STATES.has(String(run.status || "").toLowerCase()));
  if (inventoryRuns.length) {
    const keyed = new Map([...activeDiscoveryRuns(), ...inventoryRuns].map(run => [String(run.id || run.run_id || `${run.method}:${run.status}`), run]));
    state.discoveryRuns = [...keyed.values()];
  }
  if ($("#source-groups")) renderSources();
  if (state.activeView === "sources" && discoverySourceRunsActive()) pollDiscoveryMethodStatus();
}

async function loadSources({ useCached = false } = {}) {
  const generation=sourceReviewGeneration;
  const cached = useCached ? readSourcesViewCache()?.sources : null;
  if (cached && typeof cached === "object") applySourcesPayload(cached);
  try {
    const result = await api("/api/sources");
    if(generation !== sourceReviewGeneration)return;
    applySourcesPayload(result);
    writeSourcesViewCache("sources", result);
  } catch (error) {
    showToast(error.message, "error");
    clearLoadingIndicators("#source-groups, #active-discovery-source-body, #accepted-source-body");
  }
}

function runsShell() {
  $("#view-runs").innerHTML = `<div class="run-list page-body" id="run-list"><div class="panel"><p class="empty">${loadingIndicator()}</p></div></div>`;
}

let companyPurgeStartRequest = null;

async function* streamCompanyPurgePreview(endpoint, criteria, signal) {
  const response = await fetch(`${endpoint}/preview?stream=true`, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(criteria), signal,
  });
  if (!response.ok) {
    const {detail} = await response.json().catch(() => ({}));
    throw new Error(typeof detail === "string" ? detail : "The purge preview could not start.");
  }
  if (!response.headers.get("content-type")?.includes("application/x-ndjson")) {
    yield {type:"complete", result:await response.json()};
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      pending += decoder.decode(value, {stream:true});
      const lines = pending.split("\n");
      pending = lines.pop();
      for (const line of lines) {
        if (line) yield JSON.parse(line);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function openCompanyPurge(onServer) {
  const endpoint = "/api/settings/company-purge";
  const modes = {
    bottom_percent:{
      label:"Bottom percentage of ranked companies",
      detail:"Uses current shared Sales priority scores, lowest first. Rounds up to a whole company.",
      threshold:{label:"Bottom percentage", min:1, initial:10},
    },
    score_at_most:{
      label:"Shared Sales priority at or below a score",
      detail:"Uses current shared Sales priority scores. Includes the entered score.",
      threshold:{label:"Maximum score", min:0, initial:10},
    },
    other_endings:{
      label:"Domain endings outside the keep list",
      detail:"Matches the last part of the company domain, such as .com or .ai.",
    },
    unresearched:{
      label:"No saved website research",
      detail:"Matches companies with no saved website snapshots.",
    },
    repeated_missing_homepage:{
      label:"Repeated homepage 404 or 410 responses",
      detail:"Matches companies whose homepage returned only 404 or 410 on each of their latest three scan days within 90 days.",
    },
    repeated_homepage_server_errors:{
      label:"Repeated homepage server errors",
      detail:"Matches companies whose homepage returned only 5xx errors on each of their latest three scan days within 90 days.",
    },
  };
  const dialog = document.createElement("dialog");
  dialog.className = "modal company-purge-dialog";
  dialog.setAttribute("aria-labelledby", "company-purge-title");
  dialog.innerHTML = `<form class="modal-card">
    <header class="modal-head"><h2 id="company-purge-title" tabindex="-1" autofocus>Purge company data</h2>
      <button class="icon-button" type="button" data-purge-close aria-label="Close">×</button></header>
    <div class="modal-body company-purge-body">
      <div class="company-purge-progress"><strong id="company-purge-size" data-purge-size>Checking database size…</strong>
        <progress data-purge-storage-progress aria-labelledby="company-purge-size"></progress><p class="muted" data-purge-space></p></div>
      <p class="muted" data-purge-reclaim-note hidden>One-time database maintenance is needed to return reusable space to disk.</p>
      <button class="button" type="button" data-purge-reclaim hidden>Reclaim now</button>
      <fieldset class="company-purge-filters" aria-describedby="company-purge-filters-help" disabled>
        <legend>Choose purge filters</legend>
        <p id="company-purge-filters-help" class="muted">Choose all the filters you want to run together. Each matching company is counted once, and the purge reclaims disk space in one pass when available.</p>
        ${Object.entries(modes).map(([mode, {label, detail, threshold}]) => `
          <div class="company-purge-filter" data-purge-filter="${mode}">
            <label class="company-purge-choice"><input type="checkbox" value="${mode}" /><span>${label}</span></label>
            <div class="company-purge-filter-detail" hidden>
              ${threshold ? `<label class="field"><span>${threshold.label}</span>
                <input type="number" value="${threshold.initial}" min="${threshold.min}" max="100" required disabled />
              </label>` : ""}
              <p class="muted">${detail}</p>
            </div>
          </div>`).join("")}
      </fieldset>
      <div data-purge-endings hidden>
        <fieldset disabled>
          <label class="field"><span>Protected domain endings</span>
            <textarea name="endings" rows="3" maxlength="8192" spellcheck="false" aria-describedby="purge-endings-help"></textarea></label>
          <p id="purge-endings-help" class="muted">Separate endings with commas or spaces. This list applies to the domain-ending filter.</p>
          <div class="settings-actions">
            <button class="button" type="button" data-purge-endings-save disabled>Save list</button>
            <button class="button" type="button" data-purge-endings-reset>Use defaults</button>
          </div>
        </fieldset>
        <p id="company-purge-endings-status" data-purge-endings-status role="status"></p>
        <progress data-purge-endings-progress aria-labelledby="company-purge-endings-status" hidden></progress>
      </div>
      <section data-purge-preview aria-live="polite"></section>
      <label class="field" data-purge-confirmation hidden><span>Type PURGE to confirm</span>
        <input name="confirmation" autocomplete="off" spellcheck="false" maxlength="5" /></label>
      <div class="company-purge-progress" data-purge-progress hidden>
        <strong id="company-purge-progress-label"></strong>
        <progress aria-labelledby="company-purge-progress-label" aria-describedby="company-purge-progress-count"></progress>
        <small class="muted" id="company-purge-progress-count"></small>
      </div>
      <p data-purge-status role="status"></p>
      <p class="company-purge-caution"><strong>Deletion is permanent.</strong> Matching companies and their saved research, discovered contacts, and rankings will be removed from ${onServer ? "your virtual machine" : "this computer"}. Saved prospects, accounts, tagged companies, and companies with workflow history are protected. Separate copies may still contain this data.</p>
    </div>
    <footer class="modal-actions">
      <button class="button" type="button" data-purge-cancel>Cancel</button>
      <button class="button" type="submit">Preview deletion</button>
      <button class="button company-purge-delete" type="button" data-purge-delete disabled hidden>Delete permanently</button>
    </footer>
  </form>`;
  const form = $("form", dialog);
  const {confirmation} = form.elements;
  const filterGroup = $(".company-purge-filters", dialog);
  const filterControls = $$("[data-purge-filter]", filterGroup).map(row => ({
    mode: row.dataset.purgeFilter,
    check: $("input[type=checkbox]", row),
    threshold: $("input[type=number]", row),
    detail: $(".company-purge-filter-detail", row),
  }));
  const preview = $("[data-purge-preview]", dialog);
  const notice = $("[data-purge-status]", dialog);
  const progressPanel = $("[data-purge-progress]", dialog);
  const progressBar = $("progress", progressPanel);
  const progressLabel = $("#company-purge-progress-label", dialog);
  const progressCount = $("#company-purge-progress-count", dialog);
  const storageProgress = $("[data-purge-storage-progress]", dialog);
  const reclaim = $("[data-purge-reclaim]", dialog);
  const reclaimNote = $("[data-purge-reclaim-note]", dialog);
  const remove = $("[data-purge-delete]", dialog);
  const review = $("[type=submit]", dialog);
  const close = $$("[data-purge-close]", dialog);
  const cancel = $("[data-purge-cancel]", dialog);
  const confirmField = $("[data-purge-confirmation]", dialog);
  const endings = $("[data-purge-endings]", dialog);
  const editor = $("fieldset", endings);
  const endingsInput = form.elements.endings;
  const endingsSave = $("[data-purge-endings-save]", endings);
  const endingsStatus = $("[data-purge-endings-status]", endings);
  const endingsProgress = $("[data-purge-endings-progress]", endings);
  let savedEndings = null;
  let defaultEndings = "";
  let selection = null;
  let busy = false;
  let deleting = false;
  let displayedJobId;
  let loadingJob = true;
  let pollTimer;
  let previewRequest;
  let reclaimStatus;
  let reclaimAvailable;
  let reusableBytes = 0;
  let reclaiming = false;
  const purgeInProgress = job => job.status === "running" || job.status === "cancelling";
  const formatEndings = values => values.map(ending => `.${ending}`).join(", ");
  const sizeFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits:1});
  function size(bytes) {
    if (bytes >= 1e9) return `${sizeFormat.format(bytes / 1e9)} GB`;
    if (bytes >= 1e6) return `${sizeFormat.format(bytes / 1e6)} MB`;
    if (bytes >= 1e3) return `${sizeFormat.format(bytes / 1e3)} KB`;
    return `${sizeFormat.format(bytes)} B`;
  }
  function syncControls() {
    const locked = busy || deleting || loadingJob;
    reclaim.hidden = reclaimStatus !== "ready" || !(reclaimAvailable ?? (reusableBytes > 0));
    reclaim.disabled = locked;
    reclaimNote.hidden = reclaimStatus !== "needs_migration" || reusableBytes <= 0;
    filterGroup.disabled = locked;
    for (const {check, threshold, detail} of filterControls) {
      detail.hidden = !check.checked;
      if (threshold) threshold.disabled = !check.checked;
    }
    const editsEndings = filterControls.some(({mode, check}) => mode === "other_endings" && check.checked);
    endings.hidden = !editsEndings;
    const dirty = endingsInput.value !== savedEndings;
    editor.disabled = locked || savedEndings === null;
    endingsSave.disabled = editor.disabled || !dirty;
    review.disabled = locked || !filterControls.some(({check}) => check.checked)
      || (editsEndings && (savedEndings === null || dirty));
  }
  function storage(data) {
    reusableBytes = data.free_page_bytes;
    const reclaimSize = size(reusableBytes);
    reclaim.textContent = reusableBytes > 0 ? `Reclaim ${reclaimSize} now` : "Reclaim now";
    $("[data-purge-size]", dialog).textContent = Number.isSafeInteger(data.disk_bytes)
      ? `${size(data.disk_bytes)} company database on disk`
      : `${size(data.page_bytes)} company database`;
    $("[data-purge-space]", dialog).textContent = `${size(data.live_page_bytes)} in use · ${reclaimSize.replace(" ", " empty ")} can be reclaimed from your database`;
    storageProgress.hidden = true;
  }
  function showProgress(label, {processed, total} = {}, unit = "") {
    progressPanel.hidden = false;
    if (total > 0) {
      progressBar.max = total;
      progressBar.value = processed;
      const percent = Math.floor(processed * 10_000 / total) / 100;
      progressLabel.textContent = `${label} · ${percent.toFixed(2)}%`;
      progressCount.textContent = `${formatNumber(processed)} of ${formatNumber(total)} ${unit}`;
    } else {
      progressBar.removeAttribute("value");
      progressLabel.textContent = label;
      progressCount.textContent = "";
    }
  }
  function clearPreview() {
    previewRequest?.abort();
    previewRequest = null;
    selection = null;
    preview.replaceChildren();
    progressPanel.hidden = true;
    review.hidden = false;
    confirmField.hidden = remove.hidden = remove.disabled = true;
    confirmation.value = "";
  }
  function criteriaChanged() {
    notice.textContent = "";
    clearPreview();
    syncControls();
  }
  filterGroup.addEventListener("input", criteriaChanged);
  function endingsChanged() {
    clearPreview();
    notice.textContent = "";
    endingsStatus.textContent = endingsInput.value === savedEndings ? "" : "Save changes to apply this list.";
    syncControls();
  }
  endingsInput.addEventListener("input", endingsChanged);
  $("[data-purge-endings-reset]", endings).addEventListener("click", () => {
    endingsInput.value = defaultEndings;
    endingsChanged();
  });
  endingsSave.addEventListener("click", async () => {
    const edited = endingsInput.value;
    clearPreview();
    busy = true;
    syncControls();
    endingsStatus.textContent = "Saving…";
    endingsProgress.hidden = false;
    try {
      const result = await api(`${endpoint}/endings`, {method:"PUT", body:JSON.stringify({endings:edited})});
      endingsInput.value = savedEndings = formatEndings(result.kept_endings);
      endingsStatus.textContent = "Protected endings saved.";
    } catch (error) { endingsStatus.textContent = error.message; }
    finally { busy = false; endingsProgress.hidden = true; syncControls(); }
  });
  confirmation.addEventListener("input", () => { remove.disabled = confirmation.value !== "PURGE"; });
  confirmation.addEventListener("keydown", event => {
    if (event.key === "Enter") event.preventDefault();
  });
  close.forEach(button => button.addEventListener("click", () => { if (!deleting) dialog.close(); }));
  cancel.addEventListener("click", async () => {
    if (!deleting) { dialog.close(); return; }
    cancel.disabled = true;
    try {
      const jobId = displayedJobId || (await companyPurgeStartRequest)?.id;
      if (!jobId) throw new Error("Could not identify the running purge. Reopen the purge menu and try again.");
      await api(`${endpoint}/job/${encodeURIComponent(jobId)}/cancel`, {method:"POST"});
    } catch (error) {
      cancel.disabled = reclaiming;
      showToast(error.message, "error");
    }
  });
  dialog.addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    previewRequest?.abort();
    clearTimeout(pollTimer);
    dialog.remove();
  }, {once:true});
  function renderPreview(result, {filters}) {
    const more = result.companies - result.examples.length;
    const filterCounts = filters.length > 1 && result.filter_counts?.length
      ? `<p class="muted">Matches by filter can overlap.</p><ul aria-label="Matches by filter">${result.filter_counts.map(({criterion, matched}) =>
        `<li>${modes[criterion]?.label ?? "Filter"} · ${formatNumber(matched)}</li>`).join("")}</ul>` : "";
    preview.innerHTML = `<h3>${filters.length > 1 ? "Combined preview" : "Preview"}</h3><p><strong>${formatNumber(result.companies)} companies</strong> · ${formatNumber(result.candidates)} company records · ${formatNumber(result.snapshots)} website snapshots</p>
      ${Number.isSafeInteger(result.payload_bytes) ? `<p class="muted"><strong>About ${size(result.payload_bytes)}</strong> of stored research. Space returned to disk is measured after reclamation.</p>` : ""}
      ${filters.length === 1 && filters[0].criterion === "bottom_percent" ? `<p class="muted">From ${formatNumber(result.eligible)} eligible ranked companies.</p>` : ""}
      ${filterCounts}
      <ul aria-label="Examples of companies to be deleted">${result.examples.map(row => `<li>${escapeHtml(row.name)} <span class="muted">${escapeHtml(row.domain)}${row.score == null ? "" : ` · Score ${escapeHtml(row.score)}`}</span></li>`).join("")}</ul>
      ${more > 0 ? `<p class="muted">+${formatNumber(more)} more</p>` : ""}`;
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (review.hidden || review.disabled || deleting) return;
    clearPreview();
    const criteria = {filters:filterControls.filter(({check}) => check.checked)
      .map(({mode, threshold}) => ({criterion:mode, value:threshold?.valueAsNumber ?? 0}))};
    busy = true;
    syncControls();
    notice.textContent = "";
    showProgress("Checking which companies match…");
    const request = new AbortController();
    previewRequest = request;
    try {
      let result;
      for await (const event of streamCompanyPurgePreview(endpoint, criteria, request.signal)) {
        if (event.type === "error") throw new Error(event.message);
        if (event.type === "complete") { result = event.result; break; }
        if (event.type === "preview" && event.result.companies) {
          renderPreview(event.result, criteria);
          showProgress("Measuring stored research…");
        }
        if (event.type === "progress") {
          if (event.phase === "records") showProgress("Checking company records", event, "companies checked");
          if (event.phase === "storage") showProgress("Measuring stored research", event, "storage checks completed");
        }
      }
      if (!result) throw new Error("The purge preview was interrupted. Try again.");
      notice.textContent = result.companies ? "Preview expires in five minutes." : "No eligible companies match.";
      if (!result.token) return;
      selection = {...criteria, token:result.token};
      renderPreview(result, criteria);
      confirmField.hidden = remove.hidden = false;
      review.hidden = true;
    } catch (error) {
      if (!request.signal.aborted) {
        preview.replaceChildren();
        notice.textContent = error.message;
      }
    } finally {
      if (previewRequest === request) {
        previewRequest = null;
        progressPanel.hidden = true;
        busy = false;
        syncControls();
      }
    }
  });
  function beginPurgeJob(reclaimOnly = false) {
    deleting = busy = true;
    reclaiming = reclaimOnly;
    clearPreview();
    confirmation.disabled = true;
    review.hidden = true;
    remove.hidden = false;
    remove.disabled = false;
    remove.textContent = "Minimize and Continue";
    if (reclaimOnly) cancel.disabled = true;
    showProgress(reclaimOnly ? "Reclaiming disk space…" : "Preparing company data purge…");
    syncControls();
    close.forEach(button => { button.disabled = true; });
    notice.textContent = "";
  }
  function renderPurgeJob(job) {
    if (purgeInProgress(job)) {
      const filters = job.criteria?.filters ?? (job.criteria?.criterion ? [job.criteria] : []);
      if (displayedJobId !== job.id && filters.length) {
        for (const {mode, check, threshold} of filterControls) {
          const filter = filters.find(({criterion}) => criterion === mode);
          check.checked = Boolean(filter);
          if (filter && threshold) threshold.value = filter.value;
        }
      }
      if (displayedJobId !== job.id || !deleting) beginPurgeJob(job.kind === "reclaim");
      displayedJobId = job.id;
      remove.textContent = "Minimize and Continue";
      remove.disabled = false;
      notice.textContent = "";
      const {processed, total} = job.progress || {};
      if (job.status === "cancelling") {
        cancel.disabled = true;
        showProgress("Canceling deletion…");
      } else if (job.phase === "reclaiming") {
        reclaiming = cancel.disabled = true;
        showProgress(total > 0 ? "Reclaiming disk space" : "Reclaiming disk space…",
          job.progress || {}, "pages processed");
      } else if (total > 0 && processed < total) {
        const {criterion: mode, value: threshold} = filters[0] || {};
        const target = filters.length > 1 ? "companies matching the selected filters" : ({
          bottom_percent: `bottom ${threshold}% of companies`,
          score_at_most: `companies scoring ${threshold} or lower`,
          other_endings: "companies with excluded domain endings",
          unresearched: "companies without saved website research",
          repeated_missing_homepage: "companies with repeated missing homepages",
          repeated_homepage_server_errors: "companies with repeated homepage server errors",
        }[mode] || "company data");
        showProgress(`Deleting ${target}`, job.progress, "records processed");
      } else {
        showProgress(total > 0 ? "Committing deletion…" : "Preparing company data purge…");
      }
      return;
    }
    if (deleting) {
      deleting = busy = confirmation.disabled = false;
      reclaiming = false;
      displayedJobId = undefined;
      cancel.disabled = false;
      remove.textContent = "Delete permanently";
      close.forEach(button => { button.disabled = false; });
      criteriaChanged();
    }
    if (job.status === "complete") {
      const result = job.result;
      const deleted = Number.isSafeInteger(result.companies);
      if (deleted) invalidateSalesWorkbench();
      storage(result.storage);
      if (result.reclaim_status === "needs_migration") reclaimStatus = "needs_migration";
      reclaimAvailable = result.reclaim_available
        ?? (result.reclaim_status === "deferred" || reusableBytes > 0);
      const reclaimed = Number.isSafeInteger(result.reclaimed_bytes)
        ? `${size(result.reclaimed_bytes)} returned to disk.` : "";
      const reuse = result.storage.free_page_bytes > 0
        ? `${size(result.storage.free_page_bytes)} still reusable inside SQLite.` : "";
      notice.textContent = [
        deleted ? `${formatNumber(result.companies)} companies deleted.` : "",
        reclaimed, reuse, result.reclaim_error,
      ].filter(Boolean).join(" ");
      syncControls();
    } else if (job.status === "failed") {
      notice.textContent = job.error;
    } else if (job.status === "cancelled") {
      notice.textContent = "Purge canceled. No companies were deleted.";
    }
  }
  async function pollPurgeJob() {
    let running = deleting;
    try {
      await companyPurgeStartRequest?.catch(() => {});
      const job = await api(`${endpoint}/job`);
      if (!dialog.open) return;
      running = purgeInProgress(job);
      renderPurgeJob(job);
    } catch (error) {
      if (running) showProgress("Reconnecting to the purge…");
      else notice.textContent = error.message;
    }
    finally {
      if (loadingJob && !deleting) progressPanel.hidden = true;
      loadingJob = false;
      syncControls();
      if (running && dialog.open) pollTimer = setTimeout(pollPurgeJob, 1000);
    }
  }
  async function startPurgeJob(path, options, reclaimOnly = false) {
    beginPurgeJob(reclaimOnly);
    const start = api(`${endpoint}/${path}`, {method:"POST", ...options});
    companyPurgeStartRequest = start;
    try {
      const job = await start;
      if (!dialog.open) return;
      renderPurgeJob(job);
      if (purgeInProgress(job)) pollTimer = setTimeout(pollPurgeJob, 1000);
    } catch (error) {
      if (!dialog.open) { showToast(error.message, "error"); return; }
      renderPurgeJob({status:"failed", error:error.message});
      // The server may have accepted the job before the connection dropped.
      await pollPurgeJob();
    }
    finally { if (companyPurgeStartRequest === start) companyPurgeStartRequest = null; }
  }
  remove.addEventListener("click", () => {
    if (deleting) { dialog.close(); return; }
    if (!selection || confirmation.value !== "PURGE") return;
    void startPurgeJob("job", {body:JSON.stringify({...selection, confirmation:confirmation.value})});
  });
  reclaim.addEventListener("click", () => void startPurgeJob("reclaim", {}, true));
  criteriaChanged();
  document.body.append(dialog);
  dialog.showModal();
  void pollPurgeJob();
  api(endpoint).then(data => {
    if (!storageProgress.hidden) {
      storage(data.storage);
      reclaimAvailable = data.reclaim_available;
    }
    if (!reclaimStatus) reclaimStatus = data.reclaim_status;
    endingsInput.value = savedEndings = formatEndings(data.kept_endings);
    defaultEndings = formatEndings(data.default_endings);
    syncControls();
  }).catch(error => {
    if (!storageProgress.hidden) {
      $("[data-purge-size]", dialog).textContent = error.message;
      storageProgress.hidden = true;
    }
    endingsStatus.textContent = error.message;
  }).finally(() => {
    if (loadingJob && !deleting) showProgress("Checking purge status…");
  });
}

function settingsShell() {
  const storageBridge = window.webkit?.messageHandlers?.storageSettings;
  const onServer = document.documentElement.getAttribute("data-storage-mode") === "vm";
  $("#view-settings").innerHTML = `
    <div class="settings-stack page-body">
      <div class="settings-quick-grid">
        <section class="panel settings-card settings-quick-card" aria-labelledby="storage-settings-title">
          <div class="settings-body">
            <h3 id="storage-settings-title">Storage Mode</h3>
            <p>${onServer ? "Using storage on your virtual machine." : "Using storage on this computer."}</p>
            <p>${storageBridge ? "Quit and reopen the app to apply a saved change."
              : onServer ? "Start Beings Growth on this computer to open its local dashboard."
              : "Connect over SSH to use storage on your own virtual machine."}</p>
            <div class="settings-actions">
              ${storageBridge ? '<button class="button" id="open-storage-settings" type="button">Change storage mode</button>'
                : `<a class="button" href="${VM_SETUP_GUIDE}" target="_blank" rel="noopener noreferrer">VM setup guide ↗</a>`}
              <button class="button" id="open-company-purge" type="button">Purge data</button>
            </div>
          </div>
        </section>
        <section class="panel settings-card settings-quick-card" aria-labelledby="collection-tools-title">
          <div class="settings-body">
            <h3 id="collection-tools-title">Collection tools</h3>
            <p>Manage discovery sources and configure advanced scans.</p>
            <div class="settings-actions"><button class="button" type="button" data-view="sources">Sources</button></div>
          </div>
        </section>
        <section class="panel settings-card settings-quick-card" aria-labelledby="license-settings-title">
          <div class="settings-body">
            <h3 id="license-settings-title">License</h3>
            <div class="settings-actions">
              <a class="button" href="https://github.com/cameoed/growth/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">Product Use License ↗</a>
            </div>
          </div>
        </section>
      </div>
      <section class="panel settings-card scan-readiness-card">
        <div class="panel-head"><h3>Scan readiness</h3><span class="badge" id="scan-readiness-state">${loadingIndicator("Checking…")}</span></div>
        <p id="scan-readiness-tip" class="scan-readiness-tip" hidden>Public web scans are ready to use. Add OpenAI for cited search, Apify for ad discovery, Microlink for hard-to-reach pages, or DataForSEO for search visibility.</p>
        <div class="scan-readiness-list" id="scan-readiness-list" aria-live="polite"><p class="empty">Checking scan capabilities…</p></div>
      </section>
      <section class="panel credential-panel" aria-label="Provider credentials">
        <table class="credential-table"><thead><tr><th>Service</th><th>Saved credential</th><th>Replacement / actions</th></tr></thead><tbody>
          <tr class="credential-row" data-credential-service="openai"><th scope="row"><div class="credential-service"><strong>OpenAI</strong><span class="badge" id="secret-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><div class="secret-meta" id="secret-meta"></div></td><td class="credential-edit"><form id="secret-form" class="secret-form"><div class="credential-fields"><label class="field"><span>Replace key</span><input name="key" type="password" autocomplete="new-password" placeholder="Paste a new API key" required maxlength="500" /></label></div><div class="settings-actions"><button class="button primary" type="submit">Save</button><button class="button" type="button" id="remove-secret">Remove</button></div></form></td></tr>
          <tr class="credential-row" data-credential-service="openai-billing"><th scope="row"><div class="credential-service"><strong>OpenAI billing</strong><span class="badge" id="openai-billing-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><div class="secret-meta" id="openai-billing-meta"></div><a href="https://platform.openai.com/settings/organization/admin-keys" target="_blank" rel="noopener noreferrer">Create admin key ↗</a></td><td class="credential-edit"><form id="openai-billing-form" class="secret-form"><div class="credential-fields"><label class="field"><span>Admin key for Spend</span><input name="key" type="password" autocomplete="new-password" placeholder="Paste an OpenAI admin key" required maxlength="500" /></label></div><div class="settings-actions"><button class="button primary" type="submit">Save</button><button class="button" type="button" id="remove-openai-billing">Remove</button></div></form></td></tr>
          <tr class="credential-row" data-credential-service="apify"><th scope="row"><div class="credential-service"><strong>Apify</strong><span class="badge" id="apify-secret-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><div class="secret-meta" id="apify-secret-meta"></div></td><td class="credential-edit"><form id="apify-secret-form" class="secret-form"><div class="credential-fields"><label class="field"><span>Replace token</span><input name="key" type="password" autocomplete="new-password" placeholder="Paste an Apify API token" required maxlength="500" /></label></div><div class="settings-actions"><button class="button primary" type="submit">Save</button><button class="button" type="button" id="remove-apify-secret">Remove</button></div></form></td></tr>
          <tr class="credential-row" data-credential-service="dataforseo"><th scope="row"><div class="credential-service"><strong>DataForSEO</strong><span class="badge" id="dataforseo-secret-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><div class="secret-meta" id="dataforseo-secret-meta"></div></td><td class="credential-edit"><form id="dataforseo-secret-form" class="secret-form"><div class="credential-fields"><label class="field"><span>API login</span><input name="login" type="password" autocomplete="off" placeholder="DataForSEO API login" required maxlength="320" /></label><label class="field"><span>API password</span><input name="password" type="password" autocomplete="new-password" placeholder="DataForSEO API password" aria-describedby="dataforseo-trial-note" required maxlength="500" /><small id="dataforseo-trial-note">$1.00 free usage for new accounts</small></label></div><div class="settings-actions"><button class="button primary" type="submit">Save</button><button class="button" type="button" id="remove-dataforseo-secret">Remove</button></div></form></td></tr>
          <tr class="credential-row" data-credential-service="microlink"><th scope="row"><div class="credential-service"><strong>Microlink</strong><span class="badge" id="microlink-secret-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><div class="secret-meta" id="microlink-secret-meta"></div></td><td class="credential-edit"><form id="microlink-secret-form" class="secret-form"><div class="credential-fields"><label class="field"><span>Replace key</span><input name="key" type="password" autocomplete="new-password" placeholder="Paste a Microlink API key" required maxlength="500" /></label></div><div class="settings-actions"><button class="button primary" type="submit">Save</button><button class="button" type="button" id="remove-microlink-secret">Remove</button></div></form></td></tr>
          <tr class="credential-row" data-credential-service="treg"><th scope="row"><div class="credential-service"><strong>Treg</strong><span class="badge" id="people-hydration-state">${loadingIndicator("Checking…")}</span></div></th><td class="credential-summary"><span class="muted">Existing CLI login or saved token</span></td><td class="credential-edit"><div class="credential-manage"><button class="button" type="button" id="open-treg-settings">Manage Treg</button></div></td></tr>
        </tbody></table>
      </section>
      <dialog class="modal treg-modal" id="treg-settings-modal" aria-labelledby="treg-settings-title">
        <header class="modal-head"><h2 id="treg-settings-title" tabindex="-1" autofocus>Treg</h2><button type="button" class="icon-button" data-close-dialog="treg-settings-modal" aria-label="Close Treg settings">×</button></header>
        <div class="modal-body">
          <form class="secret-form" data-buyer-provider="treg">
            <div class="settings-actions"><strong>Connection</strong><span class="badge" id="buyer-provider-treg">${loadingIndicator("Checking…")}</span></div>
            <label class="field"><span>Replace token</span><input name="key" type="password" autocomplete="new-password" placeholder="Paste a token only to replace the current connection" required maxlength="500" /></label>
            <p class="form-note">An existing Treg CLI login is used automatically.</p>
            <div class="settings-actions"><button class="button" type="submit">Save token</button><button class="button" type="button" data-remove-buyer-provider="treg">Remove saved token</button></div>
          </form>
          <div class="treg-modal-grid">
      <section class="panel settings-card">
        <div class="panel-head"><h3>Buyer intelligence</h3></div>
        <div class="settings-body">
          ${[ ["tiktok", "TikTok commercial-content access token (optional)"] ].map(([provider, label]) => `
            <form class="secret-form" data-buyer-provider="${provider}">
              <label class="field"><span>${label}</span><input name="key" type="password" autocomplete="new-password" required maxlength="500" /></label>
              <div class="settings-actions"><button class="button" type="submit">Save token</button><button class="button" type="button" data-remove-buyer-provider="${provider}">Remove saved token</button><span class="badge" id="buyer-provider-${provider}">${loadingIndicator("Checking…")}</span></div>
            </form>`).join("")}
          <form id="buyer-intelligence-form" class="secret-form">
            <div class="people-hydration-fields">
              <label class="field"><span>Ranked companies</span><input name="company_limit" type="number" min="1" max="250" value="50" required /></label>
              <label class="field"><span>Run ceiling · USD</span><input name="maximum_usd" type="number" min="0.01" max="5" step="0.01" value="1" required /></label>
            </div>
          </form>
          <div id="buyer-intelligence-status" aria-live="polite"></div>
        </div>
      </section>
      <section class="panel settings-card">
        <div class="panel-head"><h3>Find people</h3></div>
        <div class="settings-body people-hydration-settings">
          <div id="people-hydration-summary" aria-live="polite"><p class="empty">Checking ranked-company people coverage…</p></div>
          <form id="people-hydration-form" class="secret-form">
            <div class="people-hydration-fields">
              <label class="field"><span>Ranked companies</span><input name="company_limit" type="number" min="1" max="2500" value="2500" required /></label>
              <label class="field"><span>People per company</span><input name="people_limit" type="number" min="2" max="12" value="8" required /></label>
            </div>
            <p class="form-note">Scans find people only. Use Find emails on a company to request work emails.</p>
          </form>
          <section id="people-hydration-quote" class="people-hydration-quote" hidden></section>
          <section id="people-hydration-run" class="people-hydration-run" aria-live="polite"></section>
          <p class="provider-data-disclaimer">Licensed professional data from Treg and its providers. A provider-verified work email does not establish permission or consent to contact.</p>
        </div>
      </section>
          </div>
        </div>
      </dialog>
    </div>`;
  if (storageBridge) {
    $("#open-storage-settings").addEventListener("click", () => storageBridge.postMessage("open"));
  }
  $("#open-company-purge").addEventListener("click", () => openCompanyPurge(onServer));
  $("#open-treg-settings").addEventListener("click", () => $("#treg-settings-modal").showModal());
  $("#secret-form").addEventListener("submit", saveSecret);
  $("#remove-secret").addEventListener("click", removeSecret);
  $("#openai-billing-form").addEventListener("submit", saveOpenAIBillingSecret);
  $("#remove-openai-billing").addEventListener("click", removeOpenAIBillingSecret);
  $("#apify-secret-form").addEventListener("submit", saveApifySecret);
  $("#remove-apify-secret").addEventListener("click", removeApifySecret);
  $("#microlink-secret-form").addEventListener("submit", saveMicrolinkSecret);
  $("#remove-microlink-secret").addEventListener("click", removeMicrolinkSecret);
  document.querySelectorAll("[data-buyer-provider]").forEach(form => form.addEventListener("submit", saveBuyerProvider));
  document.querySelectorAll("[data-remove-buyer-provider]").forEach(button => button.addEventListener("click", removeBuyerProvider));
  const peopleForm = $("#people-hydration-form");
  const refreshPeople = bindAutomaticScanEstimate(peopleForm, estimatePeopleHydration, () => {
    state.peopleHydrationQuote = null;
    $("#people-hydration-quote").hidden = true;
  });
  peopleForm.addEventListener("focusin", event => { if (!state.peopleHydrationQuote) refreshPeople(event); });
  const buyerForm = $("#buyer-intelligence-form");
  const refreshBuyer = bindAutomaticScanEstimate(buyerForm, quoteBuyerIntelligence, () => {
    buyerIntelligenceQuote = null;
    buyerIntelligenceStatus = null;
    $("#buyer-intelligence-status").replaceChildren();
  });
  buyerForm.addEventListener("focusin", event => { if (!buyerIntelligenceQuote) refreshBuyer(event); });
  $("#dataforseo-secret-form").addEventListener("submit", saveDataForSEOSecret);
  $("#remove-dataforseo-secret").addEventListener("click", removeDataForSEOSecret);
}

function normalizedReadinessItem(data, name, fallbackDescription) {
  const aliases = name === "public_web" ? ["public_web", "public"] : [name];
  const raw = firstValue(data || {}, aliases.flatMap(alias => [alias, `capabilities.${alias}`, `readiness.${alias}`, `${alias}_methods`]), null);
  if (typeof raw === "boolean") return { ready: raw, description: fallbackDescription, methods: [] };
  const item = raw && typeof raw === "object" ? raw : {};
  const methodsValue = firstValue(item, ["methods", "unlocks", "enabled_methods", "available_methods"], []);
  return {
    ...item,
    ready: Boolean(firstValue(item, ["ready", "configured", "available", "enabled"], false)),
    description: item.description || item.detail || item.message || fallbackDescription,
    methods: Array.isArray(methodsValue) ? methodsValue : (methodsValue ? [methodsValue] : []),
  };
}

function renderScanReadiness(data = state.scanReadiness) {
  const showSetup = Array.isArray(data?.missing_items) && data.missing_items.length > 0;
  const indicator = $("#scan-setup-indicator");
  if (indicator) {
    indicator.hidden = !showSetup;
    const settings = indicator.closest("button");
    if (showSetup) {
      settings.title = "Optional API setup can expand Smart Scan coverage";
      settings.setAttribute("aria-description", settings.title);
    } else {
      settings.removeAttribute("title");
      settings.removeAttribute("aria-description");
    }
  }
  const tip = $("#scan-readiness-tip");
  if (tip) tip.hidden = !showSetup;
  const root = $("#scan-readiness-list");
  const badge = $("#scan-readiness-state");
  if (!root || !badge) return;
  if (!data || data._error) {
    badge.textContent = "Unavailable";
    badge.className = "badge warn";
    root.innerHTML = `<div class="scan-readiness-error"><strong>Readiness check unavailable</strong><span>${escapeHtml(data?._error || "The local capability endpoint did not respond.")}</span><button class="button" type="button" id="retry-scan-readiness">Retry</button></div>`;
    $("#retry-scan-readiness")?.addEventListener("click", () => loadScanReadiness());
    return;
  }
  const items = [
    ["openai", "OpenAI", ""],
    ["apify", "Apify", ""],
    ["dataforseo", "DataForSEO", ""],
    ["treg", "Treg", ""],
    ["microlink", "Microlink", ""],
  ].map(([key, label, description]) => ({ key, label, ...normalizedReadinessItem(data, key, description) }));
  const publicReady = normalizedReadinessItem(data, "public_web", "").ready;
  badge.textContent = publicReady ? "Core scans ready" : "Setup needed";
  badge.className = `badge ${publicReady ? "moss" : "warn"}`;
  root.innerHTML = items.map(item => `<article class="scan-readiness-row ${item.ready ? "ready" : "optional"}" title="${escapeHtml(item.description)}"><span class="readiness-icon" aria-hidden="true">${item.ready ? "✓" : "○"}</span><strong>${escapeHtml(item.label)}</strong><span class="badge ${item.ready ? "moss" : ""}">${item.key === "treg" && item.configured ? (item.status === "authenticated" ? "Ready" : item.status === "balance_empty" ? "Add credits" : item.status === "access_denied" ? "Access denied" : item.status === "unavailable" ? "Unavailable" : "Checking…") : item.ready ? "Ready" : "Not configured"}</span></article>`).join("");
}

let scanReadinessGeneration = 0;
async function loadScanReadiness({checkTregHealth = true} = {}) {
  const generation = ++scanReadinessGeneration;
  let data;
  try {
    data = await api("/api/settings/scan-readiness");
  } catch (error) {
    data = { _error: error.message };
  }
  if (generation !== scanReadinessGeneration) return;
  state.scanReadiness = data;
  renderScanReadiness();
  syncOutboundLookupActions();
  if (checkTregHealth && data?.treg?.configured) {
    api("/api/settings/treg/health").then(health => {
      if (generation !== scanReadinessGeneration) return;
      data.treg = {...data.treg, ...health,
        detail: health.balance_microusd !== undefined ? `Balance ${spendMoney(health.balance_microusd)} · ${health.role || "member"}` : health.status};
      renderScanReadiness();
    }).catch(() => {});
  }
}

let buyerIntelligencePoll = null;
let buyerIntelligenceQuote = null;
let buyerIntelligencePending = false;
let buyerIntelligenceStatus = null;

async function loadBuyerProviderSettings() {
  await Promise.all(["treg", "tiktok"].map(async provider => {
    const badge = $(`#buyer-provider-${provider}`);
    if (!badge) return;
    try {
      const data = await api(`/api/settings/buyer-provider/${provider}`);
      badge.textContent = data.configured ? `Configured · ${data.source}` : "Not configured";
      badge.className = `badge ${data.configured ? "moss" : ""}`;
    } catch { badge.textContent = "Unavailable"; badge.className = "badge warn"; }
  }));
  if (!buyerIntelligenceQuote) {
    try {
      const latest = await api("/api/sales-engine/buyer-intelligence/status");
      if (latest.id) buyerIntelligenceQuote = latest.id;
    } catch { /* Credentials remain independently available. */ }
  }
  if (buyerIntelligenceQuote) await pollBuyerIntelligence();
}

async function saveBuyerProvider(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(`/api/settings/buyer-provider/${form.dataset.buyerProvider}`, {
      method: "PUT", body: JSON.stringify({ key: form.elements.key.value }),
    });
    form.reset();
    await loadBuyerProviderSettings();
  } catch (error) { showToast(error.message, "error"); }
}

async function removeBuyerProvider(event) {
  const provider = event.currentTarget.dataset.removeBuyerProvider;
  try {
    await api(`/api/settings/buyer-provider/${provider}`, { method: "DELETE" });
    $(`[data-buyer-provider="${provider}"]`).reset();
    await loadBuyerProviderSettings();
  } catch (error) { showToast(error.message, "error"); }
}

function renderBuyerIntelligence(data) {
  const root = $("#buyer-intelligence-status");
  if (!root) return;
  buyerIntelligenceStatus = data;
  setScanControlsLocked($("#buyer-intelligence-form"), buyerIntelligencePending || ["running", "cancel_requested"].includes(data.status));
  const dollars = maximumCostMoney(data.maximum_microusd / 1_000_000);
  const records = (data.receipts || []).reduce((sum, row) => sum + Number(row.records || 0), 0);
  root.replaceChildren();
  const line = document.createElement("p");
  line.className = "form-note";
  line.textContent = `${data.companies} companies · ${data.requests_maximum} requests max · ${dollars} max · ${data.status}${data.status === "quoted" ? "" : ` · ${records} records`}`;
  root.append(line);
  if (["running", "cancel_requested"].includes(data.status)) {
    const cancel = document.createElement("button");
    cancel.className = "button";
    cancel.textContent = data.status === "cancel_requested" ? "Stopping…" : "Stop refresh";
    cancel.disabled = buyerIntelligencePending || data.status === "cancel_requested";
    cancel.addEventListener("click", async () => {
      if (scanActionBlocked(cancel) || buyerIntelligencePending) return;
      buyerIntelligencePending = true;
      cancel.disabled = true;
      try {
        renderBuyerIntelligence(await api(`/api/sales-engine/buyer-intelligence/${data.id}/cancel`, { method: "POST" }));
      } catch (error) { showToast(error.message, "error"); }
      finally {
        buyerIntelligencePending = false;
        renderBuyerIntelligence(buyerIntelligenceStatus);
        if (["running", "cancel_requested"].includes(buyerIntelligenceStatus?.status)) void pollBuyerIntelligence();
      }
    });
    root.append(cancel);
  }
  if (data.status === "quoted" && data.requests_maximum) {
    const button = document.createElement("button");
    button.className = "button primary";
    button.textContent = `Start · ${dollars} max`;
    button.disabled = buyerIntelligencePending;
    button.addEventListener("click", async () => {
      if (scanActionBlocked(button) || buyerIntelligencePending || buyerIntelligenceStatus?.id !== data.id) return;
      buyerIntelligencePending = true;
      setScanControlsLocked($("#buyer-intelligence-form"), true);
      button.disabled = true;
      try {
        const started = await api(`/api/sales-engine/buyer-intelligence/${data.id}/start`, {
          method: "POST", body: JSON.stringify({ confirmed_maximum_microusd: data.maximum_microusd }),
        });
        renderBuyerIntelligence(started);
      } catch (error) { showToast(error.message, "error"); }
      finally {
        buyerIntelligencePending = false;
        renderBuyerIntelligence(buyerIntelligenceStatus);
        if (["running", "cancel_requested"].includes(buyerIntelligenceStatus?.status)) void pollBuyerIntelligence();
      }
    });
    root.append(button);
  }
}

async function pollBuyerIntelligence() {
  clearTimeout(buyerIntelligencePoll);
  if (buyerIntelligencePending || !buyerIntelligenceQuote || !$("#buyer-intelligence-status")) return;
  try {
    const quoteId = buyerIntelligenceQuote;
    const data = await api(`/api/sales-engine/buyer-intelligence/${quoteId}`);
    if (buyerIntelligencePending || quoteId !== buyerIntelligenceQuote) return;
    renderBuyerIntelligence(data);
    if (["running", "cancel_requested"].includes(data.status)) buyerIntelligencePoll = setTimeout(pollBuyerIntelligence, 2500);
  } catch (error) { showToast(error.message, "error"); }
}

async function quoteBuyerIntelligence(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (buyerIntelligencePending || scanControlLocks.has(form) || !form.checkValidity()) return;
  const data = await requestScanEstimate(event, "/api/sales-engine/buyer-intelligence/quote", () => ({
    company_limit: Number(form.elements.company_limit.value),
    maximum_microusd: Math.round(Number(form.elements.maximum_usd.value) * 1000000),
  }));
  if (!data) return;
  buyerIntelligenceQuote = data.id;
  renderBuyerIntelligence(data);
}

async function loadSecretSettings() {
  try {
    const data = await api("/api/settings/openai-key");
    $("#secret-state").textContent = data.configured ? "Configured" : "Not configured";
    $("#secret-state").className = `badge ${data.configured ? "moss" : ""}`;
    $("#secret-meta").textContent = data.configured ? `Key saved` : "No key is active";
  } catch (error) {
    showToast(error.message, "error");
    $("#secret-state").textContent = "Unavailable";
    $("#secret-meta").textContent = "Local access only";
  }
}

async function saveSecret(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const key = form.elements.key.value;
  const submit = form.querySelector('[type="submit"]');
  const label = submit.textContent;
  submit.disabled = true;
  submit.textContent = "Saving…";
  try {
    const result = await api("/api/settings/openai-key", { method: "PUT", body: JSON.stringify({ key }) });
    updateFirstLaunchKey(result.configured === true);
    form.reset();
    const setup = result.mission_setup;
    const completed = setup?.completed === 1 ? "Mission ready." : setup?.completed ? `${setup.completed} missions ready.` : "";
    const error = compilationErrorText(setup);
    showToast(["API key saved.", completed, error].filter(Boolean).join(" "), error ? "error" : "success");
    state.discoveryMethods = null;
    if (setup) invalidateSalesWorkbench();
    await Promise.all([loadSecretSettings(), loadScanReadiness(), setup && loadSalesMissions()]);
  } catch (error) { showToast(error.message, "error"); }
  finally { submit.disabled = false; submit.textContent = label; }
}

async function removeSecret() {
  if (!window.confirm("Remove the active OpenAI API key?")) return;
  try {
    await api("/api/settings/openai-key", { method: "DELETE" });
    updateFirstLaunchKey(false);
    $("#secret-form").reset();
    showToast("API key removed");
    state.discoveryMethods = null;
    await Promise.all([loadSecretSettings(), loadScanReadiness()]);
  } catch (error) { showToast(error.message, "error"); }
}

function renderOpenAIBillingSettings(data = {}) {
  const badge = $("#openai-billing-state");
  const meta = $("#openai-billing-meta");
  if (!badge || !meta) return;
  badge.textContent = data.configured ? "Configured" : "Not configured";
  badge.className = `badge ${data.configured ? "moss" : ""}`;
  meta.textContent = data.configured
    ? "Refresh Spend to check costs for your research key"
    : "Admin key required to look up research-key costs";
}

async function loadOpenAIBillingSettings() {
  try {
    renderOpenAIBillingSettings(await api("/api/settings/openai-admin-key"));
  } catch (error) {
    if ($("#openai-billing-state")) $("#openai-billing-state").textContent = "Unavailable";
    if ($("#openai-billing-meta")) $("#openai-billing-meta").textContent = error.message;
  }
}

async function saveOpenAIBillingSecret(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  if (button.disabled || !form.reportValidity()) return;
  button.disabled = true;
  try {
    const data = await api("/api/settings/openai-admin-key", {
      method:"PUT", body:JSON.stringify({key:form.elements.key.value}),
    });
    renderOpenAIBillingSettings(data);
    showToast("Billing key saved · Refresh Spend to read costs");
  } catch (error) { showToast(error.message, "error"); }
  finally { form.reset(); button.disabled = false; }
}

async function removeOpenAIBillingSecret() {
  if (!window.confirm("Remove the OpenAI billing key? Saved spend history will remain.")) return;
  try {
    const data = await api("/api/settings/openai-admin-key", {method:"DELETE"});
    $("#openai-billing-form").reset();
    renderOpenAIBillingSettings(data);
    showToast("Billing key removed");
  } catch (error) { showToast(error.message, "error"); }
}

function renderApifySecretSettings(data = {}) {
  const stateElement = $("#apify-secret-state");
  const metaElement = $("#apify-secret-meta");
  if (!stateElement || !metaElement) return;
  stateElement.textContent = data.configured ? "Configured" : "Not configured";
  stateElement.className = `badge ${data.configured ? "moss" : ""}`;
  metaElement.textContent = data.configured
    ? "Credential saved"
    : "No token is active";
}

async function loadApifySecretSettings() {
  try {
    renderApifySecretSettings(await api("/api/settings/apify-token"));
  } catch (error) {
    showToast(error.message, "error");
    if ($("#apify-secret-state")) $("#apify-secret-state").textContent = "Unavailable";
    if ($("#apify-secret-meta")) $("#apify-secret-meta").textContent = "Local access only";
  }
}

async function saveApifySecret(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const key = form.elements.key.value;
  try {
    const data = await api("/api/settings/apify-token", { method: "PUT", body: JSON.stringify({ key }) });
    form.reset();
    renderApifySecretSettings(data);
    showToast("Apify token saved");
    state.discoveryMethods = null;
    await loadScanReadiness();
    if ($("#paid-discovery-form")) {
      await loadAdDiscoveryOverview();
      const prepared = prepareMetaExistingBacklogDraft();
      if (prepared) showToast(`Apify ready · ${formatNumber(prepared)} Meta targets due`);
    }
  } catch (error) { showToast(error.message, "error"); }
}

async function removeApifySecret() {
  if (!window.confirm("Remove the active Apify API token? Paid-ad scans will remain disabled.")) return;
  try {
    const data = await api("/api/settings/apify-token", { method: "DELETE" });
    $("#apify-secret-form").reset();
    renderApifySecretSettings(data);
    showToast("Apify token removed");
    state.discoveryMethods = null;
    await loadScanReadiness();
    if ($("#paid-discovery-form")) await loadAdDiscoveryOverview();
  } catch (error) { showToast(error.message, "error"); }
}

function renderMicrolinkSecretSettings(data = {}) {
  const stateElement = $("#microlink-secret-state");
  const metaElement = $("#microlink-secret-meta");
  if (!stateElement || !metaElement) return;
  stateElement.textContent = data.configured ? "Configured" : "Not configured";
  stateElement.className = `badge ${data.configured ? "moss" : ""}`;
  metaElement.textContent = data.configured
    ? "Credential saved"
    : "No key is active; direct public retrieval remains available";
}

async function loadMicrolinkSecretSettings() {
  try {
    renderMicrolinkSecretSettings(await api("/api/settings/microlink-key"));
  } catch (error) {
    showToast(error.message, "error");
    if ($("#microlink-secret-state")) $("#microlink-secret-state").textContent = "Unavailable";
    if ($("#microlink-secret-meta")) $("#microlink-secret-meta").textContent = "Local access only";
  }
}

async function saveMicrolinkSecret(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const key = form.elements.key.value;
  try {
    const data = await api("/api/settings/microlink-key", {
      method: "PUT",
      body: JSON.stringify({ key }),
    });
    form.reset();
    renderMicrolinkSecretSettings(data);
    showToast("Microlink key saved");
    await loadScanReadiness();
  } catch (error) { showToast(error.message, "error"); }
}

async function removeMicrolinkSecret() {
  if (!window.confirm("Remove the active Microlink API key? Blocked pages will use direct retrieval only.")) return;
  try {
    const data = await api("/api/settings/microlink-key", { method: "DELETE" });
    $("#microlink-secret-form").reset();
    renderMicrolinkSecretSettings(data);
    showToast("Microlink key removed");
    await loadScanReadiness();
  } catch (error) { showToast(error.message, "error"); }
}

function renderPeopleHydration(data = state.peopleHydration) {
  const badge = $("#people-hydration-state");
  const summary = $("#people-hydration-summary");
  const runRoot = $("#people-hydration-run");
  const form = $("#people-hydration-form");
  if (!badge || !summary || !runRoot || !form) return;
  if (!data || data._error) {
    [...form.elements].forEach(element => { element.disabled = true; });
    $("#people-hydration-quote").hidden = true;
    if ($("#start-people-hydration")) $("#start-people-hydration").disabled = true;
    badge.textContent = "Unavailable";
    badge.className = "badge warn";
    summary.innerHTML = `<p class="empty">${escapeHtml(data?._error || "People hydration status did not respond.")}</p>`;
    return;
  }
  const inventory = data.inventory || {};
  const run = data.run || {};
  const active = ACTIVE_RUN_STATES.has(String(run.status || "").toLowerCase());
  badge.textContent = data.configured ? (active ? "Running" : "Configured") : "Setup required";
  badge.className = `badge ${data.configured ? "moss" : "warn"}`;
  summary.innerHTML = `<div class="people-hydration-metrics">
    <div><small>Ranked cohort</small><strong>${formatNumber(inventory.cohort_count || 0)}</strong></div>
    <div><small>Fresh 90d</small><strong>${formatNumber(inventory.fresh_companies || 0)}</strong></div>
    <div><small>Needs more</small><strong>${formatNumber(inventory.thin_companies || 0)}</strong></div>
    <div><small>Due</small><strong>${formatNumber(inventory.due_companies || 0)}</strong></div>
    <div><small>People</small><strong>${formatNumber(inventory.people_count || 0)}</strong></div>
    <div><small>Work emails</small><strong>${formatNumber(inventory.verified_email_count || 0)}</strong></div>
  </div>${data.configured ? "" : `<p class="form-note">Treg is not available to the dashboard process. Authenticate with the official Treg CLI or set TREG_TOKEN, then restart the dashboard.</p>`}`;
  [...form.elements].forEach(element => { element.disabled = active || state.peopleHydrationStarting || scanControlLocks.has(form) || !data.configured; });
  if (["running", "complete", "failed", "cancelled"].includes(run.status)) {
    const processed = Number(run.processed_companies || 0);
    const total = Number(run.company_count || 0);
    const percent = total ? Math.min(100, 100 * processed / total) : Number(run.progress_percent || 0);
    runRoot.innerHTML = `<div class="people-run-head"><strong>${escapeHtml(run.message || humanize(run.status))}</strong><span class="badge ${run.status === "complete" ? "moss" : run.status === "failed" ? "warn" : ""}">${escapeHtml(humanize(run.status))}</span></div>
      <div class="people-run-progress"><i style="width:${percent}%"></i></div>
      <p>${formatNumber(processed)} / ${formatNumber(total)} companies · ${formatNumber(run.people_found || 0)} people · ${formatNumber(run.completed_companies || 0)} held 90d · ${formatNumber(run.verified_emails || 0)} verified work emails · ${run.cost_unknown ? "Unknown actual cost" : `${money(run.actual_cost_usd || 0)} actual`}</p>
      ${run.current_company ? `<small>Now: ${escapeHtml(run.current_company)}</small>` : ""}`;
  } else {
    runRoot.replaceChildren();
  }
}

async function loadPeopleHydrationStatus() {
  if (!$("#people-hydration-summary")) return;
  const observedRun = state.peopleHydration?.run;
  try {
    const data = await api("/api/people-hydration/status");
    if (state.peopleHydrationStarting || state.peopleHydration?.run !== observedRun) return;
    state.peopleHydration = data;
  } catch (error) {
    state.peopleHydration = { _error: error.message };
  }
  renderPeopleHydration();
  if (state.peopleHydration?.run?.status === "running") pollPeopleHydration();
}

async function estimatePeopleHydration(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!state.peopleHydration?.configured || state.peopleHydrationStarting
      || scanControlLocks.has(form) || !form.checkValidity()) return;
  const data = await requestScanEstimate(event, "/api/people-hydration/estimate", () => ({
    company_limit:Number(form.elements.company_limit.value),
    people_limit:Number(form.elements.people_limit.value),
    email_limit:0,
  }));
  if (!data) return;
  state.peopleHydrationQuote = data;
  renderPeopleHydrationQuote();
}

function renderPeopleHydrationQuote() {
  const root = $("#people-hydration-quote");
  const quote = state.peopleHydrationQuote;
  if (!root || !quote) return;
  root.hidden = false;
  root.innerHTML = `<div class="panel-head"><h4>Confirmed ceiling required</h4><span class="badge warn">Up to ${maximumCostMoney(quote.maximum_usd)}</span></div>
    <div class="people-hydration-metrics">
      <div><small>Due companies</small><strong>${formatNumber(quote.due_companies || 0)}</strong></div>
      <div><small>Fresh reused</small><strong>${formatNumber(quote.fresh_companies || 0)}</strong></div>
      <div><small>Discovery max</small><strong>${maximumCostMoney(quote.discovery_max_usd)}</strong></div>
    </div>
    <p>${escapeHtml(quote.cost_note || "The run cannot exceed this quote.")}</p>
    <button class="button primary" type="button" id="start-people-hydration" ${quote.due_companies && !state.peopleHydrationStarting && !scanControlLocks.has($("#people-hydration-form")) && !ACTIVE_RUN_STATES.has(state.peopleHydration?.run?.status) ? "" : "disabled"}>Confirm ${maximumCostMoney(quote.maximum_usd)} max and run</button>`;
  $("#start-people-hydration")?.addEventListener("click", startPeopleHydration);
}

async function startPeopleHydration() {
  const quote = state.peopleHydrationQuote;
  const form = $("#people-hydration-form");
  if (!quote || !quote.due_companies || !state.peopleHydration?.configured || state.peopleHydration?._error
      || state.peopleHydrationStarting || scanControlLocks.has(form)
      || scanActionBlocked($("#start-people-hydration")) || ACTIVE_RUN_STATES.has(state.peopleHydration?.run?.status)) return;
  state.peopleHydrationStarting = true;
  state.peopleHydration = { ...state.peopleHydration, run: {
    status: "starting", message: "Starting people scan…", progress_percent: 0,
    company_count: quote.due_companies, processed_companies: 0,
    started_at: new Date().toISOString(),
  }};
  setScanControlsLocked(form, true);
  const button = $("#start-people-hydration");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  $("#treg-settings-modal")?.close();
  renderAggregateScanState();
  void setView("smart_scan").catch(error => showToast(error.message, "error"));
  try {
    const run = await api("/api/people-hydration/runs", {
      method: "POST",
      body: JSON.stringify({
        quote_id: quote.quote_id,
        confirmed: true,
        confirmed_max_usd: quote.maximum_usd,
      }),
    });
    state.peopleHydration = { ...(state.peopleHydration || {}), run };
    state.peopleHydrationQuote = null;
    if ($("#people-hydration-quote")) $("#people-hydration-quote").hidden = true;
    if (state.scanOverview) state.scanOverview.people = run;
    renderPeopleHydration();
    renderAggregateScanState();
    showToast(run.message || "People scan started");
    pollPeopleHydration();
  } catch (error) {
    state.peopleHydration = { ...state.peopleHydration, run: {
      status: "failed", message: `Could not start people scan: ${error.message}`,
      started_at: state.peopleHydration?.run?.started_at,
      finished_at: new Date().toISOString(),
    }};
    if (state.scanOverview) state.scanOverview.people = state.peopleHydration.run;
    renderAggregateScanState();
    showToast(error.message, "error");
    if (button) { button.disabled = false; button.textContent = `Confirm ${maximumCostMoney(quote.maximum_usd)} max and run`; }
  } finally {
    state.peopleHydrationStarting = false;
    setScanControlsLocked(form, false);
    renderPeopleHydration();
    renderAggregateScanState();
  }
}

async function pollPeopleHydration() {
  if (state.peopleHydrationPollInFlight) return;
  clearTimeout(state.peopleHydrationPollTimer);
  state.peopleHydrationPollInFlight = true;
  try {
    const previous = state.peopleHydration?.run?.status;
    const observedRun = state.peopleHydration?.run;
    const data = await api("/api/people-hydration/status");
    if (state.peopleHydrationStarting || state.peopleHydration?.run !== observedRun) {
      state.peopleHydrationPollTimer = setTimeout(pollPeopleHydration, 2500);
      return;
    }
    state.peopleHydration = data;
    if (state.scanOverview) state.scanOverview.people = data.run;
    renderPeopleHydration();
    renderAggregateScanState();
    if (state.peopleHydration.run?.status === "running") {
      state.peopleHydrationPollTimer = setTimeout(pollPeopleHydration, 2500);
    } else if (previous === "running") {
      showToast(state.peopleHydration.run?.message || "People hydration finished", scanToastTone(state.peopleHydration.run?.status));
      await refreshVisibleData();
    }
  } catch {
    state.peopleHydrationPollTimer = setTimeout(pollPeopleHydration, 4000);
  } finally {
    state.peopleHydrationPollInFlight = false;
  }
}

function renderDataForSEOSecretSettings(data = {}) {
  const stateElement = $("#dataforseo-secret-state");
  const metaElement = $("#dataforseo-secret-meta");
  if (!stateElement || !metaElement) return;
  stateElement.textContent = data.configured ? "Configured" : "Not configured";
  stateElement.className = `badge ${data.configured ? "moss" : ""}`;
  metaElement.textContent = data.configured
    ? "Credentials saved"
    : "No credentials are active; paid SEO correction will be skipped";
}

async function loadDataForSEOSecretSettings() {
  try {
    renderDataForSEOSecretSettings(await api("/api/settings/dataforseo-credentials"));
  } catch (error) {
    showToast(error.message, "error");
    if ($("#dataforseo-secret-state")) $("#dataforseo-secret-state").textContent = "Unavailable";
    if ($("#dataforseo-secret-meta")) $("#dataforseo-secret-meta").textContent = "Local access only";
  }
}

async function saveDataForSEOSecret(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const login = form.elements.login.value;
  const password = form.elements.password.value;
  try {
    const data = await api("/api/settings/dataforseo-credentials", {
      method: "PUT",
      body: JSON.stringify({ login, password }),
    });
    form.reset();
    renderDataForSEOSecretSettings(data);
    showToast("DataForSEO credentials saved");
    await Promise.all([loadScanReadiness(), loadSmartScan()]);
  } catch (error) { showToast(error.message, "error"); }
}

async function removeDataForSEOSecret() {
  if (!window.confirm("Remove the active DataForSEO credentials? Smart Scan will skip paid SEO correction.")) return;
  try {
    const data = await api("/api/settings/dataforseo-credentials", { method: "DELETE" });
    $("#dataforseo-secret-form").reset();
    renderDataForSEOSecretSettings(data);
    showToast("DataForSEO credentials removed");
    await Promise.all([loadScanReadiness(), loadSmartScan()]);
  } catch (error) { showToast(error.message, "error"); }
}

function canonicalDiscoveryTabId(value = "", label = "") {
  const token = `${value} ${label}`.toLowerCase().replace(/[^a-z0-9]+/g, "_");
  if ((token.includes("web") && token.includes("ct")) || token === "web") return "web_ct";
  if (token.includes("company_intelligence") || token.includes("company_scanner")) return "company_intelligence";
  if (token.includes("tenant")) return "tenant_footprints";
  if (token.includes("director") || token.includes("agency_directory") || token.includes("brand_directory")) return "directories";
  if (token.includes("portfolio")) return "portfolios";
  if (token.includes("customer") || token.includes("platform_customer")) return "customer_lists";
  if (token.includes("search")) return "searches";
  if (token.includes("graph")) return "public_graph";
  if (token.includes("paid") || token.includes("advert")) return "paid_ads";
  if (token.includes("scout") || token.includes("find_source")) return "find_sources";
  return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "_");
}

function discoveryTabs() {
  const supplied = Array.isArray(state.discoveryMethods?.tabs) ? state.discoveryMethods.tabs : [];
  return DISCOVERY_TAB_DEFINITIONS.map(definition => {
    const server = supplied.find(item => canonicalDiscoveryTabId(item.id, item.label) === definition.id) || {};
    return {
      ...definition,
      ...server,
      id: definition.id,
      label: definition.label,
      source_kinds: server.source_kinds || definition.source_kinds,
      api_id: server.id || definition.id,
      sources: Array.isArray(server.sources) ? server.sources : [],
      scanners: Array.isArray(server.scanners) ? server.scanners : [],
    };
  });
}

function discoveryTabListMarkup() {
  return `<div class="discovery-tab-shell"><div class="discovery-tabs" role="tablist" aria-label="Scan method">
    ${discoveryTabs().filter(tab => tab.id !== "find_sources").map(tab => `<button class="discovery-tab" id="discovery-tab-${tab.id}" type="button" role="tab" aria-controls="discovery-panel-${tab.id}" aria-selected="${state.discoveryTab === tab.id}" tabindex="${state.discoveryTab === tab.id ? "0" : "-1"}" data-discovery-tab="${tab.id}" data-search-text="${escapeHtml(tab.label)}"><span>${escapeHtml(tab.label)}</span><small id="discovery-tab-meta-${tab.id}">—</small><small class="discovery-tab-failure" id="discovery-tab-failure-${tab.id}">No scans yet</small><span class="discovery-tab-progress" id="discovery-tab-progress-${tab.id}" role="progressbar" aria-label="${escapeHtml(tab.label)} scan progress" aria-valuemin="0" aria-valuemax="100" aria-hidden="true"><i></i></span></button>`).join("")}
  </div></div>`;
}

function discoveryReliability(tab = {}) {
  const reliability = tab.reliability || {};
  const attempts = Math.max(0, Number(reliability.attempt_count || 0));
  const failures = Math.max(0, Number(reliability.failure_count || 0));
  const rate = attempts ? Math.max(0, Math.min(100, Number(reliability.failure_rate || 0))) : 0;
  return { attempts, failures, rate };
}

function discoveryReliabilityBadge(tab = {}) {
  const reliability = discoveryReliability(tab);
  if (!reliability.attempts) return `<span class="badge">No scan history</span>`;
  const tone = reliability.rate >= 50 ? "warn" : reliability.rate === 0 ? "moss" : "";
  return `<span class="badge ${tone}">${reliability.rate.toFixed(1)}% failed · ${formatNumber(reliability.failures)} / ${formatNumber(reliability.attempts)} scans</span>`;
}

function discoveryMethodHeader(tab, badgeMarkup) {
  return `<div class="panel-head"><div class="scan-card-title"><h3>${escapeHtml(tab.label)}</h3>${scanCardHelp(tab.id, tab.label, SCAN_METHOD_HELP[tab.id])}</div><div class="discovery-method-badges">${badgeMarkup}</div></div>`;
}

function discoveryPanelAttributes(tabId) {
  return `class="discovery-tab-panel" id="discovery-panel-${tabId}" role="tabpanel" aria-labelledby="discovery-tab-${tabId}" data-discovery-panel="${tabId}"${state.discoveryTab === tabId ? "" : " hidden"}`;
}

function discoveryMethodPanelShell(tab) {
  return `<section ${discoveryPanelAttributes(tab.id)}><div class="discovery-method-root" id="discovery-method-${tab.id}"><section class="panel discovery-method-loading"><p class="empty">${loadingIndicator(`Loading ${tab.label.toLowerCase()}…`)}</p></section></div></section>`;
}

function bindDiscoveryTabs() {
  $$("[data-discovery-tab]").forEach(button => {
    button.addEventListener("click", () => selectDiscoveryTab(button.dataset.discoveryTab, {focus:true}));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = $$("[data-discovery-tab]").filter(tab => !tab.hidden);
      const current = tabs.indexOf(event.currentTarget);
      const target = event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectDiscoveryTab(tabs[target].dataset.discoveryTab, { focus: true });
    });
  });
}

function selectDiscoveryTab(tabId, { focus = false } = {}) {
  const sectionLink = $$(".scan-section-link").find(button => button.dataset.discoveryTab === tabId);
  if (!sectionLink && !discoveryTabs().some(tab => tab.id === tabId)) return;
  if (tabId === "find_sources" && focus && $("#find-directories-dialog")) {
    $("#smart-scan-options-dialog")?.close();
    const dialog = $("#find-directories-dialog");
    if (!dialog.open) dialog.showModal();
    return;
  }
  const buttons = $$("[data-discovery-tab]");
  const unavailable = new Set(buttons.filter(button => button.hidden).map(button => button.dataset.discoveryTab));
  if (unavailable.has(tabId)) {
    tabId = buttons.find(button => !button.hidden && !button.classList.contains("scan-section-link"))?.dataset.discoveryTab
      || buttons.find(button => !button.hidden)?.dataset.discoveryTab;
  }
  state.discoveryTab = tabId;
  buttons.forEach(button => {
    const selected = button.dataset.discoveryTab === tabId;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = !button.hidden && ($("#scan-methods-content") || selected) ? 0 : -1;
    if (selected && focus) button.focus();
  });
  const continuous = Boolean($("#scan-methods-content"));
  if (continuous) {
    $(".discovery-tabs")?.setAttribute("role", "group");
    $$("[data-discovery-tab]").forEach(button => button.removeAttribute("role"));
    $$("[data-discovery-panel]").forEach(panel => panel.setAttribute("role", "region"));
  }
  $$("[data-discovery-panel]").forEach(panel => { panel.hidden = unavailable.has(panel.dataset.discoveryPanel) || (!continuous && panel.dataset.discoveryPanel !== tabId); });
  if (continuous && focus) {
    const sectionId = sectionLink?.getAttribute("aria-controls") || `discovery-panel-${tabId}`;
    $(`#${sectionId}`)?.scrollIntoView({behavior:"smooth", block:"start"});
    refreshSelectedScanEstimate();
  }
}

function smartScanActive(scan = state.smartScan) {
  return ["starting", "queued", "running", "retrying", "reconnecting", "pausing", "paused", "restarting", "stopping"].includes(String(scan?.status || "").toLowerCase());
}

function smartScanInitializing(scan) {
  return state.smartScanStarting || scan?.preparing === true;
}

function smartScanBusy(scan = state.smartScan) {
  return smartScanActive(scan) || scan?.running === true || scan?.cleaning_up === true;
}

function selectedSourceAI() {
  return Boolean(state.smartScanDueHydrationEnabled && (state.smartScan?.profile?.source_ai_control_available
    ? state.smartScanSourceAIEnabled : state.smartScanAIEnabled));
}

function selectedMicrolink() {
  return Boolean(state.smartScanDueHydrationEnabled && state.smartScan?.profile?.microlink_control_available
    && state.smartScan.profile.microlink_configured && state.smartScanMicrolinkEnabled);
}

function microlinkHydrationOptions() {
  const saved = state.smartScanMicrolinkHydration || {};
  return Object.fromEntries([
    ...["contacts", "company_pages", "web_search", "news_search", "documents",
      "technologies", "media", "business_listings", "browser_interactions"]
      .map(key => [key, Boolean(saved[key])]),
    ["company_limit", Number(saved.company_limit ?? 100)],
    ["request_limit", Number(saved.request_limit ?? 500)],
  ]);
}

function selectedMicrolinkHydration() {
  const options = microlinkHydrationOptions();
  return Boolean(state.smartScan?.profile?.microlink_hydration_available
    && state.smartScan.profile.microlink_configured && ["contacts", "company_pages", "web_search", "news_search", "documents",
      "technologies", "media", "business_listings"].some(key => options[key]));
}

function scanCardHelp(id, label, ...paragraphs) {
  const tooltipId = `scan-help-${id}`;
  return `<button class="scan-card-info" type="button" aria-label="About ${escapeHtml(label)}"
    aria-describedby="${tooltipId}">i</button><div class="scan-card-tooltip" id="${tooltipId}"
    role="tooltip" popover="manual">${paragraphs.map(text => `<p>${escapeHtml(text)}</p>`).join("")}</div>`;
}

const SCAN_FIELD_HELP = {
  "smart-scan-use-source-ai": "Use OpenAI to find and validate sources within the scan’s usage caps.",
  "smart-scan-use-microlink": "Use Microlink allowance to render websites when direct retrieval fails.",
  "smart-scan-discovery-terms": "Guide discovery with up to five search terms, one per line.",
  "smart-scan-ml-contacts": "Read rendered contact and leadership pages for public contact details.",
  "smart-scan-ml-browser_interactions": "Open public disclosures such as Show more. Uses the shared request limit.",
  "smart-scan-ml-company_pages": "Read first-party pages for hiring, launches, and other sales signals.",
  "smart-scan-ml-documents": "Read supported public documents linked from the company site.",
  "smart-scan-ml-technologies": "Identify technologies used on the company website.",
  "smart-scan-ml-media": "Look for creative assets and related metadata on the website.",
  "smart-scan-ml-web_search": "Look for additional public evidence about ranked companies.",
  "smart-scan-ml-news_search": "Look for recent company news that may signal buying timing.",
  "smart-scan-ml-business_listings": "Look for public listings that help confirm company identity.",
  lane: "Choose the mission or buying signals that guide company search.",
  prefer_large: "Favor companies with evidence of at least 50 employees or enterprise scale.",
  force_contacts: "Allow contact checks for companies that were checked recently.",
  urls: "Paste company websites separated by lines, spaces, or commas.",
  free_intelligence: "Collect public website evidence for company scoring.",
  agency_discovery: "Check public signals that identify agencies and their services.",
  people_hydration_enabled: "Use the paid people provider to find relevant contacts.",
  microlink_enabled: "Allow rendered website fallback using Microlink allowance.",
  ai_resolution: "Use OpenAI to resolve source results and review candidates within the limits above.",
  mode: "Choose whether to discover advertisers, enrich existing targets, or do both.",
  meta_enabled: "Include Meta Ad Library research through Apify.",
  tiktok_library_enabled: "Search TikTok’s Commercial Content Library using Apify.",
  tiktok_top_enabled: "Include TikTok Top Ads research through Apify.",
  verify_landing_domains: "Check whether ad landing domains belong to the advertiser.",
  hydrate_websites: "Inspect verified company websites for sales evidence.",
};

const SCAN_METHOD_HELP = {
  web_ct: "Discover domains through public web sources and certificate records.",
  company_intelligence: "Run selected research scanners on existing companies.",
  tenant_footprints: "Find companies through tenant domains on shared platforms.",
  directories: "Crawl approved directories for company websites.",
  portfolios: "Find companies listed in approved brand group portfolios.",
  customer_lists: "Find companies named in approved platform customer lists.",
  searches: "Run saved searches to discover company candidates.",
  public_graph: "Discover companies through public business and location datasets.",
  paid_ads: "Use Apify to find advertisers and collect company-linked ad evidence.",
};

let scanHelpFieldId = 0;
function addScanFieldHelp(root) {
  for (const label of root.querySelectorAll("label")) {
    const input = label.control;
    if (!input || input.type === "hidden" || label.closest(".scan-field-help")
      || label.nextElementSibling?.matches(".scan-card-info")) continue;
    const caption = label.querySelector("span")?.textContent.trim() || label.textContent.trim();
    const key = input.id || input.name;
    const help = SCAN_FIELD_HELP[`${input.form?.id}:${key}`] || SCAN_FIELD_HELP[key]
      || (!key && caption.startsWith("English") ? "This scan requires English-presenting websites." : "");
    if (!help) continue;
    const field = document.createElement("div");
    field.className = "scan-field-help";
    if (label.matches(".paid-query-field, .scan-setup-scope")) field.classList.add("scan-field-help-wide");
    label.before(field);
    field.append(label);
    field.insertAdjacentHTML("beforeend", scanCardHelp(`field-${++scanHelpFieldId}`, caption, help));
  }
}

function microlinkHydrationMarkup() {
  const checkbox = (key, label) => `<label class="smart-scan-ai-toggle"><input type="checkbox"
    id="smart-scan-ml-${key}" data-microlink-option="${key}" /><span>${label}</span></label>`;
  return `<section id="smart-scan-microlink-options" class="scan-option-group" aria-labelledby="smart-scan-microlink-heading" hidden>
    <h3 id="smart-scan-microlink-heading">Microlink research</h3>
    <div class="scan-option-rows">
      <div class="scan-option-row scan-microlink-row" role="group" aria-label="Contacts via Microlink">
        <div class="scan-card-title"><span>Contacts</span>
        ${scanCardHelp("contacts", "Contacts via Microlink", "Read public contact and leadership pages using Microlink allowance.")}
        </div>
        <div class="scan-option-checks">
          ${checkbox("contacts", "Contacts & leadership")}
          ${checkbox("browser_interactions", "Expand dynamic content")}
        </div>
      </div>
      <div class="scan-option-row scan-microlink-row" role="group" aria-label="Company evidence via Microlink">
        <div class="scan-card-title"><span>Company evidence</span>
        ${scanCardHelp("company-evidence", "Company evidence via Microlink", "Collect first-party company evidence, documents, technologies, and media via Microlink.")}
        </div>
        <div class="scan-option-checks">
          ${checkbox("company_pages", "Company pages")}
          ${checkbox("documents", "Public documents")}
          ${checkbox("technologies", "Technology stack")}
          ${checkbox("media", "Creative media")}
        </div>
      </div>
      <div class="scan-option-row scan-microlink-row" role="group" aria-label="Search &amp; timing via Microlink">
        <div class="scan-card-title"><span>Search &amp; timing</span>
        ${scanCardHelp("search-timing", "Search & timing via Microlink", "Find web, news, and listing evidence for ranked companies within your Microlink allowance.")}
        </div>
        <div class="scan-option-checks">
          ${checkbox("web_search", "Web search")}
          ${checkbox("news_search", "News search")}
          ${checkbox("business_listings", "Business listings")}
        </div>
      </div>
      <div class="scan-option-row scan-microlink-usage">
        <div class="scan-option-copy">
          <div class="scan-card-title"><span>Shared usage limits</span>${scanCardHelp("microlink-limits", "Shared usage limits", "These limits apply across all selected Microlink research options.")}</div>
          <small id="smart-scan-ml-usage" class="scan-option-detail">Uses Microlink allowance</small>
        </div>
        <div class="scan-option-fields">
          <label class="smart-scan-ai-limit"><span>Top companies</span><input id="smart-scan-ml-companies"
            type="number" inputmode="numeric" min="1" max="10000" step="1" value="100" /></label>
          <label class="smart-scan-ai-limit"><span>Max requests</span><input id="smart-scan-ml-requests"
            type="number" inputmode="numeric" min="1" max="45000" step="1" value="500" /></label>
        </div>
      </div>
    </div>
  </section>`;
}

function smartScanEstimatedCost(profile = {}, siteLimit = state.smartScanAISiteLimit) {
  const quoted = state.smartScanPaidEstimate;
  if (quoted && quoted.ai_sites === Number(siteLimit)
      && quoted.due_hydration === Boolean(state.smartScanDueHydrationEnabled)
      && (quoted.source_ai == null || quoted.source_ai === selectedSourceAI())) {
    return Number(quoted.ai.estimate_usd);
  }
  const estimateValue = (value, fallback) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  const maximumSites = estimateValue(profile.candidate_ai_call_max, SMART_SCAN_AI_ESTIMATE_DEFAULTS.maximumSites);
  const limit = Math.max(0, Math.min(maximumSites, Number(siteLimit) || 0));
  const sourceToolCalls = estimateValue(profile.source_ai_tool_call_cap, SMART_SCAN_AI_ESTIMATE_DEFAULTS.sourceToolCalls);
  const sourceCostPerToolCall = estimateValue(profile.source_ai_estimate_per_tool_call_usd, SMART_SCAN_AI_ESTIMATE_DEFAULTS.sourceCostPerToolCall);
  const candidateCostPerSite = estimateValue(profile.candidate_ai_estimate_per_site_usd, SMART_SCAN_AI_ESTIMATE_DEFAULTS.candidateCostPerSite);
  const candidateWebSites = estimateValue(profile.candidate_ai_web_call_cap, SMART_SCAN_AI_ESTIMATE_DEFAULTS.candidateWebSites);
  const candidateWebCostPerSite = estimateValue(profile.candidate_ai_web_estimate_per_site_usd, SMART_SCAN_AI_ESTIMATE_DEFAULTS.candidateWebCostPerSite);
  const source = selectedSourceAI() ? Number(profile.source_ai_estimate_usd ?? sourceToolCalls * sourceCostPerToolCall) : 0;
  const candidate = limit * candidateCostPerSite;
  const web = Math.min(limit, candidateWebSites) * candidateWebCostPerSite;
  return Math.round((source + candidate + web + Number.EPSILON) * 100) / 100;
}

function smartScanSEOLimits(profile = {}, siteLimit = state.smartScanSEOSiteLimit) {
  const maximum = Number(profile.dataforseo_target_max || 100000);
  const targetLimit = Math.max(1, Math.min(maximum, Number(siteLimit) || 1));
  const detailDivisor = Number(profile.dataforseo_detail_divisor || 20);
  const bulkBatchLimit = Number(profile.dataforseo_bulk_batch_limit || 1000);
  const detailLimit = Math.ceil(targetLimit / detailDivisor);
  const requestCap = Math.ceil(targetLimit / bulkBatchLimit) + detailLimit;
  return { maximum, targetLimit, detailLimit, requestCap };
}

function smartScanSEOEstimate(scan = state.smartScan || {}, { forLaunch = false } = {}) {
  const profile = scan.profile || {};
  const active = smartScanActive(scan);
  const terminal = ["complete", "partial", "failed", "stopped"].includes(String(scan.status || "").toLowerCase());
  const useFrozenRun = !forLaunch && !scan.preparing && (active || terminal) && "dataforseo_enabled" in scan;
  const included = useFrozenRun
    ? Boolean(scan.dataforseo_enabled)
    : Boolean(state.smartScanSEOEnabled && (scan.inventory?.dataforseo_configured ?? profile.dataforseo_configured));
  if (!included) return 0;
  const frozenEstimate = Number(scan.estimated_dataforseo_cost_usd);
  if (useFrozenRun && Number.isFinite(frozenEstimate) && frozenEstimate >= 0) return frozenEstimate;
  const selectedLimit = useFrozenRun
    ? Number(scan.seo_site_limit || scan.counters?.dataforseo_target_cap || profile.dataforseo_target_default || 1000)
    : state.smartScanSEOSiteLimit;
  if (!useFrozenRun && state.smartScanPaidEstimate?.seo_sites === Number(selectedLimit)) {
    return Number(state.smartScanPaidEstimate.seo.estimate_usd);
  }
  const { targetLimit, detailLimit, requestCap } = smartScanSEOLimits(profile, selectedLimit);
  const bulkRequests = requestCap - detailLimit;
  const bulkTaskCost = Number(profile.dataforseo_bulk_task_estimate_usd || .012);
  const bulkItemCost = Number(profile.dataforseo_bulk_item_estimate_usd || .00012);
  const detailTaskCost = Number(profile.dataforseo_detail_task_estimate_usd || .01212);
  return Math.round((bulkRequests * bulkTaskCost + targetLimit * bulkItemCost + detailLimit * detailTaskCost) * 100) / 100;
}

function smartScanPeopleEstimate(scan = state.smartScan || {}, { forLaunch = false } = {}) {
  const active = smartScanActive(scan);
  const terminal = ["complete", "partial", "failed", "stopped"].includes(String(scan.status || "").toLowerCase());
  const frozen = !forLaunch && !scan.preparing && (active || terminal) && "people_hydration_enabled" in scan;
  const configured = Boolean(scan.inventory?.people_hydration_configured ?? scan.profile?.people_hydration_configured);
  if (frozen && !scan.people_hydration_enabled) return 0;
  if (!frozen && (!configured || !state.smartScanPeopleEnabled)) return 0;
  if (!frozen && scan.profile?.people_limit_control_available) {
    return state.smartScanPaidEstimate?.people_sites === Number(state.smartScanPeopleCompanyLimit)
      ? Number(state.smartScanPaidEstimate.people?.estimate_usd || 0) : 0;
  }
  const estimate = Number(frozen ? scan.estimated_people_hydration_cost_usd : scan.inventory?.people_hydration_estimate_usd);
  return Number.isFinite(estimate) && estimate >= 0 ? estimate : 0;
}

function scheduleSmartScanPaidEstimate() {
  const ai_sites = state.smartScanAIEnabled ? Number(state.smartScanAISiteLimit) : 0;
  const seo_sites = state.smartScanSEOEnabled ? Number(state.smartScanSEOSiteLimit) : 0;
  const due_hydration = Boolean(state.smartScanDueHydrationEnabled);
  const source_ai = selectedSourceAI();
  const microlink = selectedMicrolink();
  const ads_sites = state.smartScanAdsEnabled ? Number(state.smartScanAdsCompanyLimit) : 0;
  const ads_max_usd = state.smartScanAdsEnabled ? Number(state.smartScanAdsMaxUSD) : 5;
  const params = new URLSearchParams({ai_sites, seo_sites, due_hydration, mission_id:selectedScanMission()});
  if (state.smartScan?.profile?.paid_ads_included) {
    params.set("ads_sites", String(ads_sites));
    params.set("ads_max_usd", String(ads_max_usd));
  }
  if (state.smartScan?.profile?.source_ai_control_available) params.set("source_ai", String(source_ai));
  if (state.smartScan?.profile?.microlink_control_available) params.set("microlink", String(microlink));
  const people_sites = state.smartScanPeopleEnabled ? Number(state.smartScanPeopleCompanyLimit) : 0;
  if (state.smartScan?.profile?.people_limit_control_available) params.set("people_sites", String(people_sites));
  const key = params.toString();
  if (state.smartScanPaidEstimateKey === key) return;
  state.smartScanPaidEstimateKey = key;
  if (state.smartScanPaidEstimate) {
    state.smartScanPreviousPaidEstimate = state.smartScanPaidEstimate;
  }
  state.smartScanPaidEstimate = null;
  state.smartScanPaidEstimateError = "";
  clearTimeout(state.smartScanPaidEstimateTimer);
  const generation = (state.smartScanPaidEstimateGeneration || 0) + 1;
  state.smartScanPaidEstimateGeneration = generation;
  state.smartScanPaidEstimateTimer = setTimeout(async () => {
    try {
      const data = await api(`/api/smart-scan/paid-estimate?${params}`);
      if (generation !== state.smartScanPaidEstimateGeneration
          || key !== state.smartScanPaidEstimateKey) return;
      state.smartScanPaidEstimate = {...data, ai_sites, seo_sites, due_hydration, source_ai, microlink, people_sites, ads_sites, ads_max_usd};
    } catch (error) {
      if (generation !== state.smartScanPaidEstimateGeneration
          || key !== state.smartScanPaidEstimateKey) return;
      state.smartScanPaidEstimateError = error.message;
    }
    updateSmartScanLaunchControls();
  }, 350);
}

function renderScanLauncherStatus(scan = state.smartScan || {}) {
  const shell = $("#smart-scan-shell");
  if (!shell) return;
  const launcher = $("#scan-launch-trigger", shell);
  const initial = shell.dataset.initialLoading === "true";
  const summary = state.scanOverview?.smart;
  const smart = summary && (initial || Date.parse(summary.updated_at) > Date.parse(scan.updated_at))
    ? summary : scan;
  const starting = smartScanInitializing(smart);
  const active = !smart.cleaning_up && (starting || smartScanBusy(smart));
  const paused = smart.status === "paused" || smart.control_status === "paused";
  const rows = scanOverviewItems();
  const source = (scan.external_source_runs ?? state.scanOverview?.methods)?.[0];
  const standalone = rows.find(row => !row.parent && ACTIVE_RUN_STATES.has(row.status));
  const microlink = state.microlinkScanActive ? state.microlinkOverview : null;
  const label = starting ? "Starting Smart Scan…"
    : active ? paused ? "Smart Scan paused" : "Smart Scan is running..."
    : microlink ? `${microlink.label} is running…`
    : standalone ? `${standalone.label} is running…`
    : source ? `${source.method_label || humanize(source.method || "Public-source scan")} is running…` : "";
  const waiting = !label && (initial || state.smartScanLoading
    || shell.dataset.microlinkStatusPending === "true" || shell.dataset.overviewPending === "true");
  const unavailable = waiting && shell.dataset.scanStatusUnavailable === "true";
  launcher.dataset.running = String(Boolean(label));
  const text = label || (waiting ? unavailable ? "Scan status unavailable" : "Checking scan status…" : "Smart Scan");
  if (launcher.textContent !== text) launcher.textContent = text;
  launcher.disabled = waiting || Boolean(label) || Boolean(scanLaunchConflict("smart", scan));
  if (state.openSmartScanWhenReady && state.activeView === "smart_scan" && (!waiting || unavailable)) {
    state.openSmartScanWhenReady = false;
    if (!launcher.disabled && !$("#smart-scan-options-dialog")?.open) launcher.click();
  }
  shell.dataset.launchStatusPending = String(waiting);
  if (initial) {
    $("#smart-scan-run-controls", shell).hidden = !active;
    const pauseResume = $("#pause-resume-smart-scan", shell);
    pauseResume.textContent = paused ? "Resume" : "Pause";
    pauseResume.disabled = true;
    $("#stop-smart-scan", shell).disabled = true;
  }
}

function updateSmartScanLaunchControls(scan = state.smartScan || {}) {
  syncDiscoveryProviderChoices(scan);
  const profile = scan.profile || {};
  const inventory = scan.inventory || {};
  const active = smartScanActive(scan);
  const locked = smartScanLaunchBlocked(state.smartScan ? scan : null);
  const hydrationCheckbox = $("#smart-scan-due-hydration");
  const adsCheckbox = $("#smart-scan-use-ads");
  const adsInput = $("#smart-scan-ads-sites");
  const adsCap = $("#smart-scan-ads-cap");
  const peopleCheckbox = $("#smart-scan-use-people");
  const peopleInput = $("#smart-scan-people-sites");
  const checkbox = $("#smart-scan-use-ai");
  const sourceCheckbox = $("#smart-scan-use-source-ai");
  const microlinkCheckbox = $("#smart-scan-use-microlink");
  const input = $("#smart-scan-ai-sites");
  const seoCheckbox = $("#smart-scan-use-seo");
  const seoInput = $("#smart-scan-seo-sites");
  const failureCheckbox = $("#smart-scan-skip-failures");
  const failureInput = $("#smart-scan-failure-rate");
  const button = $("#start-smart-scan");
  if (!hydrationCheckbox || !checkbox || !input || !seoCheckbox || !seoInput || !failureCheckbox || !failureInput || !button) return;
  const openAIConfigured = (inventory.openai_configured ?? profile.openai_configured) !== false;
  const dataForSEOConfigured = Boolean(inventory.dataforseo_configured ?? profile.dataforseo_configured);
  const peopleConfigured = Boolean(inventory.people_hydration_configured ?? profile.people_hydration_configured);
  const adsConfigured = Boolean(inventory.apify_configured ?? profile.paid_ads_configured);
  const microlinkConfigured = Boolean(profile.microlink_configured);
  if (!active && !scan.preparing) {
    if (!openAIConfigured) {
      state.smartScanAIEnabled = false;
      state.smartScanSourceAIEnabled = false;
    }
    if (!dataForSEOConfigured) state.smartScanSEOEnabled = false;
    if (!adsConfigured) state.smartScanAdsEnabled = false;
    if (!peopleConfigured) state.smartScanPeopleEnabled = false;
    if (profile.microlink_configured === false) {
      state.smartScanMicrolinkEnabled = false;
      state.smartScanMicrolinkHydration = {};
    }
  }
  if (active && !scan.preparing) {
    state.smartScanDueHydrationEnabled = scan.due_hydration_enabled !== false;
    state.smartScanDueCompanyLimit = scan.due_company_limit ?? null;
    state.smartScanAdsEnabled = Boolean(scan.paid_ads_enabled);
    state.smartScanAdsCompanyLimit = Number(scan.paid_ads_company_limit || 100);
    state.smartScanAdsMaxUSD = Number(scan.paid_ads_max_usd || 5);
    state.smartScanPeopleEnabled = Boolean(scan.people_hydration_enabled);
    state.smartScanPeopleCompanyLimit = Number(scan.people_company_limit || profile.people_hydration_cohort_limit || 2500);
    state.smartScanAIEnabled = Boolean(scan.ai_enabled);
    state.smartScanSourceAIEnabled = Boolean(scan.source_ai_enabled ?? scan.ai_enabled);
    state.smartScanMicrolinkEnabled = Boolean(scan.microlink_enabled);
    state.smartScanMicrolinkHydration = {...(scan.microlink_hydration || {})};
    state.smartScanAISiteLimit = active ? Number(scan.ai_site_limit || profile.candidate_ai_call_cap || 100) : 100;
    state.smartScanSEOEnabled = Boolean(scan.dataforseo_enabled);
    state.smartScanSEOSiteLimit = Number(scan.seo_site_limit || scan.counters?.dataforseo_target_cap || profile.dataforseo_target_default || 1000);
    state.smartScanFailureSkipEnabled = scan.max_company_failure_rate != null;
    state.smartScanFailureRate = Number(scan.max_company_failure_rate ?? 75);
  }
  const show = (selector, visible) => {
    const element = $(selector);
    if (element) element.hidden = !visible;
  };
  show(".smart-scan-ai-card", openAIConfigured || active && state.smartScanAIEnabled);
  show(".smart-scan-seo-card", dataForSEOConfigured || active && state.smartScanSEOEnabled);
  show(".smart-scan-ads-card", adsConfigured || active && state.smartScanAdsEnabled);
  show(".smart-scan-people-card", peopleConfigured || active && state.smartScanPeopleEnabled);
  const shell = $("#smart-scan-shell");
  const standalone = '[data-scan-setup="discovery"], [data-scan-setup="contacts"], '
    + 'input[name="scan-setup-mode"]:not([value="smart"])';
  shell?.querySelectorAll?.(standalone).forEach(control => {
    (control.closest("label") || control).hidden = profile.microlink_configured === false;
  });
  const lockNote = $("#smart-scan-settings-lock");
  if (lockNote) {
    const scanInProgress = smartScanBusy(scan) || state.microlinkScanActive || scan.external_source_runs?.length;
    lockNote.textContent = scanInProgress
      ? "Settings are locked while a scan is in progress."
      : scan.inventory_error ? "Inventory is unavailable. Retry the inventory check from Scan." : "";
    lockNote.hidden = !locked || !lockNote.textContent;
  }
  show("#smart-scan-provider-note", !openAIConfigured || !dataForSEOConfigured
    || !adsConfigured || !peopleConfigured || !microlinkConfigured);
  const refreshLimit = $("#smart-scan-due-limit");
  if (refreshLimit) {
    refreshLimit.value = state.smartScanDueCompanyLimit ?? "";
    refreshLimit.disabled = locked || !state.smartScanDueHydrationEnabled;
  }
  const goalSend = $("#scan-goal-form button");
  if (goalSend) {
    goalSend.disabled = Boolean(state.smartScanGoalPending || locked || !openAIConfigured);
    goalSend.setAttribute("aria-busy", String(Boolean(state.smartScanGoalPending)));
    goalSend.title = !openAIConfigured ? "Connect OpenAI in Settings" : "Configure scan with AI · $0.02 max";
  }
  const maximum = Number(profile.candidate_ai_call_max || 100);
  const seoMaximum = Number(profile.dataforseo_target_max || 100000);
  input.max = String(maximum);
  seoInput.max = String(seoMaximum);
  if (document.activeElement !== input || active) input.value = String(state.smartScanAISiteLimit);
  if (document.activeElement !== seoInput || active) seoInput.value = String(state.smartScanSEOSiteLimit);
  hydrationCheckbox.checked = state.smartScanDueHydrationEnabled;
  hydrationCheckbox.disabled = locked;
  const searchTerms = $("#smart-scan-discovery-terms");
  if (searchTerms) {
    searchTerms.disabled = locked || !state.smartScanDueHydrationEnabled;
    searchTerms.closest?.("label")?.toggleAttribute?.("hidden", !state.smartScanDueHydrationEnabled);
    show("#smart-scan-terms-help", state.smartScanDueHydrationEnabled);
    const value = active && !scan.preparing
      ? (scan.search_terms || []).join("\n") : state.smartScanSearchTermsDraft || "";
    if (searchTerms.value !== value) searchTerms.value = value;
  }
  if (peopleCheckbox) {
    peopleCheckbox.checked = state.smartScanPeopleEnabled;
    peopleCheckbox.disabled = locked || !peopleConfigured;
  }
  checkbox.checked = state.smartScanAIEnabled;
  seoCheckbox.checked = state.smartScanSEOEnabled;
  failureCheckbox.checked = state.smartScanFailureSkipEnabled;
  if (document.activeElement !== failureInput || active) failureInput.value = String(state.smartScanFailureRate);
  if (peopleInput) {
    peopleInput.max = String(profile.people_hydration_cohort_limit || 2500);
    if (document.activeElement !== peopleInput || active) peopleInput.value = String(state.smartScanPeopleCompanyLimit);
    peopleInput.disabled = locked || !state.smartScanPeopleEnabled || !peopleConfigured;
  }
  if (adsCheckbox && adsInput && adsCap) {
    adsCheckbox.checked = state.smartScanAdsEnabled;
    adsInput.value = String(state.smartScanAdsCompanyLimit);
    adsCap.value = String(state.smartScanAdsMaxUSD);
    adsCheckbox.disabled = locked || !adsConfigured;
    adsInput.disabled = locked || !adsConfigured || !state.smartScanAdsEnabled;
    adsCap.disabled = locked || !adsConfigured || !state.smartScanAdsEnabled;
  }
  const validAds = Number.isInteger(state.smartScanAdsCompanyLimit)
    && state.smartScanAdsCompanyLimit >= 1 && state.smartScanAdsCompanyLimit <= 500
    && Number.isFinite(state.smartScanAdsMaxUSD) && state.smartScanAdsMaxUSD >= .10
    && state.smartScanAdsMaxUSD <= 100
    && Math.abs(state.smartScanAdsMaxUSD * 100 - Math.round(state.smartScanAdsMaxUSD * 100)) < 1e-8;
  const rawPeopleLimit = Number(peopleInput?.value || state.smartScanPeopleCompanyLimit || 100);
  const validPeopleLimit = Number.isInteger(rawPeopleLimit) && rawPeopleLimit >= 1
    && rawPeopleLimit <= Number(profile.people_hydration_cohort_limit || 2500);
  const rawLimit = Number(input.value);
  const validLimit = Number.isInteger(rawLimit) && rawLimit >= 1 && rawLimit <= maximum;
  const rawSEOLimit = Number(seoInput.value);
  const validSEOLimit = Number.isInteger(rawSEOLimit) && rawSEOLimit >= 1 && rawSEOLimit <= seoMaximum;
  const rawFailureRate = Number(failureInput.value);
  const validFailureRate = Number.isFinite(rawFailureRate) && rawFailureRate >= 0 && rawFailureRate <= 100;
  checkbox.disabled = locked || !openAIConfigured;
  if (sourceCheckbox) {
    sourceCheckbox.closest?.("label")?.toggleAttribute("hidden", !profile.source_ai_control_available
      || !state.smartScanDueHydrationEnabled || !(openAIConfigured || active && state.smartScanSourceAIEnabled));
    sourceCheckbox.checked = selectedSourceAI();
    sourceCheckbox.disabled = locked || !openAIConfigured || !state.smartScanDueHydrationEnabled;
  }
  if (microlinkCheckbox) {
    microlinkCheckbox.closest?.("label")?.toggleAttribute("hidden", !profile.microlink_control_available
      || !state.smartScanDueHydrationEnabled || !(microlinkConfigured || active && state.smartScanMicrolinkEnabled));
    microlinkCheckbox.checked = active && !scan.preparing
      ? Boolean(scan.microlink_enabled) : selectedMicrolink();
    microlinkCheckbox.disabled = locked || !profile.microlink_configured || !state.smartScanDueHydrationEnabled;
  }
  const mlOptions = profile.microlink_hydration_available
    ? microlinkHydrationOptions() : {company_limit:100, request_limit:500};
  const mlSelected = Boolean(profile.microlink_hydration_available && selectedMicrolinkHydration());
  const mlRoot = $("#smart-scan-microlink-options");
  const hasMicrolinkWork = Object.values(mlOptions).some(value => value === true);
  const showMicrolink = profile.microlink_hydration_available
    && (microlinkConfigured || active && hasMicrolinkWork);
  if (mlRoot) mlRoot.hidden = !showMicrolink;
  if (mlRoot && showMicrolink) {
    mlRoot.querySelectorAll("[data-microlink-option]").forEach(control => {
      const key = control.dataset.microlinkOption;
      control.checked = mlOptions[key];
      const isDynamicContent = key === "browser_interactions";
      const hideDynamicContent = isDynamicContent && !mlOptions.contacts;
      if (isDynamicContent) control.closest("label").hidden = hideDynamicContent;
      control.disabled = locked || !profile.microlink_configured || hideDynamicContent;
    });
    for (const [id, key] of [["companies", "company_limit"], ["requests", "request_limit"]]) {
      const control = $(`#smart-scan-ml-${id}`);
      if (document.activeElement !== control || active) control.value = String(mlOptions[key]);
      control.closest("label").hidden = !(mlSelected || active && hasMicrolinkWork);
      control.disabled = locked || !profile.microlink_configured || !mlSelected;
    }
    $("#smart-scan-ml-usage").textContent = !profile.microlink_configured
      ? "Add a Microlink key in Settings"
      : mlSelected
        ? `Up to ${formatNumber(mlOptions.request_limit)} requests · subscription usage · fallback separate`
        : "Uses Microlink allowance";
  }
  const mlValid = Number.isInteger(mlOptions.company_limit) && mlOptions.company_limit >= 1
    && mlOptions.company_limit <= 10000 && Number.isInteger(mlOptions.request_limit)
    && mlOptions.request_limit >= 1 && mlOptions.request_limit <= 45000;
  input.disabled = locked || !state.smartScanAIEnabled || !openAIConfigured;
  seoCheckbox.disabled = locked || !dataForSEOConfigured;
  seoInput.disabled = locked || !state.smartScanSEOEnabled || !dataForSEOConfigured;
  failureCheckbox.disabled = locked;
  failureInput.disabled = locked || !state.smartScanFailureSkipEnabled;
  failureInput.closest?.("label")?.toggleAttribute?.("hidden", !state.smartScanFailureSkipEnabled);
  const hasSelection = state.smartScanDueHydrationEnabled || mlSelected
    || state.smartScanPeopleEnabled && peopleConfigured || state.smartScanAIEnabled
    || state.smartScanSEOEnabled || state.smartScanAdsEnabled && adsConfigured;
  button.disabled = locked
    || !hasSelection
    || (mlSelected && (!mlValid || !profile.microlink_configured))
    || (state.smartScanAdsEnabled && (!validAds || !adsConfigured))
    || (state.smartScanPeopleEnabled && !validPeopleLimit)
    || (state.smartScanAIEnabled && (!validLimit || !openAIConfigured))
    || (state.smartScanSEOEnabled && (!validSEOLimit || !dataForSEOConfigured))
    || (state.smartScanFailureSkipEnabled && !validFailureRate);
  const aiEstimate = (state.smartScanAIEnabled && validLimit) || selectedSourceAI()
    ? smartScanEstimatedCost(profile, state.smartScanAIEnabled ? rawLimit : 0) : 0;
  const paidNeeded = !active && !scan.preparing
    && profile.paid_reuse_estimate_available === true
    && (state.smartScanAIEnabled || state.smartScanSEOEnabled || selectedSourceAI() || selectedMicrolink()
      || state.smartScanAdsEnabled
      || (state.smartScanPeopleEnabled && profile.people_limit_control_available))
    && (!state.smartScanAIEnabled || validLimit)
    && (!state.smartScanSEOEnabled || validSEOLimit)
    && (!state.smartScanPeopleEnabled || validPeopleLimit)
    && (!state.smartScanAdsEnabled || validAds);
  if (paidNeeded) scheduleSmartScanPaidEstimate();
  const paidChecking = paidNeeded && !state.smartScanPaidEstimate
    && !state.smartScanPaidEstimateError;
  if (paidChecking) button.disabled = true;
  // Keep matching row estimates visible without using them to price a launch.
  const previousQuote = state.smartScanPreviousPaidEstimate;
  const displayQuote = state.smartScanPaidEstimate ?? (paidChecking ? previousQuote : null);
  const pendingLabel = previousQuote ? "Updating estimate…" : "Checking estimate…";
  function showEstimateStatus(element, label, pending = false) {
    if (element.textContent !== label) element.textContent = label;
    element.setAttribute("data-estimate-pending", String(pending));
  }
  const aiQuote = displayQuote?.ai_sites === rawLimit ? displayQuote.ai : null;
  const seoQuote = displayQuote?.seo_sites === rawSEOLimit ? displayQuote.seo : null;
  const peopleQuote = displayQuote?.people_sites === rawPeopleLimit ? displayQuote.people : null;
  const adsQuote = displayQuote?.ads_sites === Number(state.smartScanAdsCompanyLimit)
    && displayQuote?.ads_max_usd === Number(state.smartScanAdsMaxUSD) ? displayQuote.ads : null;
  const sourceQuote = displayQuote?.due_hydration === Boolean(state.smartScanDueHydrationEnabled)
    && (displayQuote.source_ai === selectedSourceAI() || (displayQuote.source_ai == null
      && displayQuote.ai_sites === (state.smartScanAIEnabled ? rawLimit : 0))) ? displayQuote.ai : null;
  const frozenAI = active && !scan.preparing && Number.isFinite(Number(scan.estimated_ai_cost_usd));
  const displayAITotal = frozenAI ? Number(scan.estimated_ai_cost_usd) : aiEstimate;
  const sourceAIFallback = selectedSourceAI()
    ? Number(profile.source_ai_estimate_usd ?? Number(profile.source_ai_tool_call_cap || 10)
      * Number(profile.source_ai_estimate_per_tool_call_usd || .02)) : 0;
  const sourceAIEstimate = Math.min(displayAITotal, Number(
    frozenAI ? scan.estimated_source_ai_cost_usd ?? sourceAIFallback
      : sourceQuote?.source_estimate_usd ?? sourceAIFallback));
  const reviewAIEstimate = Number(frozenAI
    ? scan.estimated_ai_review_cost_usd ?? Math.max(0, displayAITotal - sourceAIEstimate)
    : aiQuote?.review_estimate_usd ?? Math.max(0, displayAITotal - sourceAIEstimate));
  const seoEstimate = state.smartScanSEOEnabled && validSEOLimit ? smartScanSEOEstimate(scan, { forLaunch: true }) : 0;
  const peopleEstimate = smartScanPeopleEstimate(scan);
  const peopleLaunchEstimate = smartScanPeopleEstimate(scan, { forLaunch: true });
  const adsEstimate = state.smartScanAdsEnabled && validAds ? state.smartScanAdsMaxUSD : 0;
  const estimate = Math.round((aiEstimate + seoEstimate + peopleLaunchEstimate + adsEstimate) * 100) / 100;
  const adsFrozen = active && !scan.preparing;
  const adsEnabled = adsFrozen ? Boolean(scan.paid_ads_enabled) : state.smartScanAdsEnabled;
  const adsCost = $("#smart-scan-ads-cost");
  if (adsCost) {
    adsCost.hidden = !adsEnabled;
    const adsDue = adsQuote?.due;
    const checkingAds = adsEnabled && !adsFrozen && validAds && paidChecking && !adsQuote;
    showEstimateStatus(adsCost, adsFrozen
      ? `Actual ${money(Number(scan.actual_paid_ads_cost_usd || 0))} · Max ${money(Number(scan.paid_ads_max_usd || 0))}`
      : !validAds ? "Check the company limit and spend cap"
      : checkingAds ? `Max ${money(adsEstimate)} · ${pendingLabel}`
      : `Max ${money(adsEstimate)}${adsDue == null ? "" : ` · ${formatNumber(adsDue)} due`}`, checkingAds);
  }

  const unpricedUsage = active ? Boolean(scan.has_unpriced_provider_usage)
    : mlSelected
      || selectedMicrolink() && Boolean(state.smartScanPaidEstimate?.has_unpriced_provider_usage);
  const estimatingCostLabel = "Estimating scan cost…";
  let launchLabel = scan.cleaning_up
    ? "Cleanup in progress"
    : !active && scan.external_source_runs?.length
    ? "Public-source scan running"
    : state.smartScanStarting || scan.preparing
    ? "Starting…"
    : active
      ? String(scan.status || "").toLowerCase() === "paused"
        ? "Smart Scan paused"
        : String(scan.status || "").toLowerCase() === "reconnecting"
          ? "Waiting for internet"
          : "Scanning..."
      : state.smartScanLoading
        ? "Loading scan settings…"
      : !hasSelection
        ? "Select a scan"
      : paidChecking
        ? estimatingCostLabel
        : "Scan";
  if (!button.disabled) {
    const costText = unpricedUsage ? `Known ${money(estimate)} + usage caps`
      : estimate === 0 ? "$0.00"
      : state.smartScanAdsEnabled && estimate === adsEstimate ? `Up to ${money(estimate)}`
      : `Est. ${money(estimate)}${state.smartScanAdsEnabled ? " incl. ad cap" : ""}`;
    launchLabel += ` · ${costText}`;
  }
  button.setAttribute("aria-busy", String(Boolean(state.smartScanLoading || state.smartScanStarting || scan.preparing || paidChecking)));
  showEstimateStatus(button, launchLabel, launchLabel === estimatingCostLabel);
  const launcher = $("#scan-launch-trigger");
  if (launcher) {
    const smartConflict = scanLaunchConflict("smart", scan);
    const microlinkLocked = Boolean(scanLaunchConflict("microlink", scan));
    const importLocked = (!scan.cleaning_up && state.smartScanLoading) || Boolean(scanLaunchConflict("import", scan));
    renderScanLauncherStatus(scan);
    const running = launcher.dataset.running === "true";
    shell?.querySelectorAll?.('[data-scan-setup="discovery"], [data-scan-setup="contacts"]').forEach(control => {
      control.disabled = microlinkLocked;
    });
    const launchNote = $("#scan-launch-lock");
    const launchMessage = shell?.dataset.launchStatusPending === "true" ? ""
      : scan.cleaning_up && !importLocked ? "Smart Scan is finishing cleanup. Import can still save companies."
      : running ? "" : smartConflict;
    launchNote.hidden = !launchMessage;
    if (launchNote.textContent !== launchMessage) launchNote.textContent = launchMessage;
    $("#import-companies-trigger").disabled = importLocked;
    const importSubmit = $("#start-import-scan");
    if (importSubmit) {
      importSubmit.disabled = importLocked;
      if (!state.importScanStarting) importSubmit.textContent = scan.cleaning_up ? "Import companies" : "Import and scan · $0.00";
    }
    const retry = $("#retry-scan-inventory");
    retry.hidden = !scan.inventory_error || active;
    retry.disabled = locked;
  }
  const estimateLabel = $("#smart-scan-ai-estimate");
  if (estimateLabel) {
    estimateLabel.hidden = !state.smartScanAIEnabled;
    const checkingAI = openAIConfigured && state.smartScanAIEnabled && validLimit && paidChecking && !aiQuote;
    showEstimateStatus(estimateLabel, !openAIConfigured
      ? "Add an OpenAI key in Settings"
      : state.smartScanAIEnabled && validLimit
        ? checkingAI ? pendingLabel
          : `Estimated reviews ${money(reviewAIEstimate)}${aiQuote ? ` · ${formatNumber(aiQuote.reused)} reused · ${formatNumber(aiQuote.due)} due` : ""}`
        : "Check the site limit", checkingAI);
  }
  const sourceAILabel = $("#smart-scan-source-ai-estimate");
  if (sourceAILabel) {
    const checkingSource = paidChecking && selectedSourceAI() && !sourceQuote;
    sourceAILabel.hidden = !openAIConfigured || (!checkingSource && sourceAIEstimate <= 0);
    showEstimateStatus(sourceAILabel, checkingSource ? pendingLabel : `Source AI est. ${money(sourceAIEstimate)}`, checkingSource);
  }
  const microlinkCost = $("#smart-scan-microlink-cost");
  if (microlinkCost) {
    microlinkCost.hidden = !unpricedUsage || !selectedMicrolink();
    microlinkCost.textContent = "Microlink pricing unavailable · scan limits apply";
  }
  const seoEstimateLabel = $("#smart-scan-seo-estimate");
  if (seoEstimateLabel) {
    seoEstimateLabel.hidden = !state.smartScanSEOEnabled;
    if (active && !scan.preparing && scan.dataforseo_enabled) {
      const {detailLimit} = smartScanSEOLimits(profile, rawSEOLimit);
      const detailCap = Number(scan.dataforseo_detail_limit || scan.counters?.dataforseo_detail_cap || detailLimit);
      showEstimateStatus(seoEstimateLabel, `Actual ${money(Number(scan.actual_dataforseo_cost_usd || 0))} · est. ${money(smartScanSEOEstimate(scan))} · ${formatNumber(rawSEOLimit)} bulk + ${formatNumber(detailCap)} detail`);
    } else {
      const checkingSEO = validSEOLimit && paidChecking && !seoQuote;
      showEstimateStatus(seoEstimateLabel, !validSEOLimit ? "Check the site limit"
        : checkingSEO ? pendingLabel
        : `Estimated SEO total ${money(seoQuote?.estimate_usd ?? seoEstimate)}${seoQuote ? ` · ${formatNumber(seoQuote.reused)} reused · ${formatNumber(seoQuote.bulk_due)} bulk + ${formatNumber(seoQuote.detail_due)} detail due` : ""}`, checkingSEO);
    }
  }
  const peopleScope = $("#smart-scan-people-scope");
  if (peopleScope) {
    const peopleDue = Number(peopleQuote?.due ?? inventory.people_hydration_due_companies ?? 0);
    const frozenPeople = active && !scan.preparing && "people_hydration_enabled" in scan;
    const peopleEnabled = frozenPeople ? Boolean(scan.people_hydration_enabled) : peopleConfigured && state.smartScanPeopleEnabled;
    const peopleActual = Number(scan.actual_people_hydration_cost_usd || 0);
    peopleScope.hidden = !peopleEnabled && !(scan.inventory_pending && !frozenPeople);
    const checkingInventory = scan.inventory_pending && !frozenPeople;
    const checkingPeople = !checkingInventory && peopleEnabled && !frozenPeople && validPeopleLimit && paidChecking && !peopleQuote;
    showEstimateStatus(peopleScope, checkingInventory
      ? scan.inventory_error ? "Coverage unavailable" : "Checking coverage…"
      : !peopleEnabled ? ""
      : frozenPeople
        ? `Actual ${scan.people_hydration?.cost_unknown ? "Unknown" : money(peopleActual)} · est. ${money(peopleEstimate)} · ${formatNumber(scan.counters?.people_hydration_due || peopleDue)} due`
      : !validPeopleLimit ? "Check the company limit"
      : checkingPeople ? pendingLabel
      : `Estimated people ${maximumCostMoney(peopleQuote?.estimate_usd ?? peopleLaunchEstimate)} · ${formatNumber(peopleQuote?.reused || 0)} reused · ${formatNumber(peopleDue)} due`, checkingPeople);
  }
}

function smartScanContentMarkup() {
  const countLoader = loadingIndicator("Checking available companies…", {iconOnly:true});
  return `
    <div class="panel smart-scan-controls" id="smart-scan-controls">
    <div class="smart-scan-head">
      <button class="button smart-scan-button" id="scan-launch-trigger" type="button" data-scan-setup="smart" data-verbatim
        aria-haspopup="dialog" aria-controls="smart-scan-options-dialog" disabled>Checking scan status…</button>
      <div class="smart-scan-run-controls" id="smart-scan-run-controls" hidden>
        <button class="button" id="pause-resume-smart-scan" type="button" value="pause">Pause</button>
        <button class="button danger" id="stop-smart-scan" type="button">Stop</button>
      </div>
      <div class="smart-scan-choice-divider">OR</div>
      <div class="scan-shortcuts">
        <button class="button" data-scan-setup="discovery" type="button" aria-haspopup="dialog" aria-controls="find-companies-dialog" disabled>Find companies</button>
        <button class="button" id="import-companies-trigger" type="button" aria-haspopup="dialog" aria-controls="import-companies-dialog" disabled>Import companies</button>
        <button class="button" id="find-directories-trigger" type="button" aria-haspopup="dialog" aria-controls="find-directories-dialog" disabled>Find directories</button>
        <button class="button" data-scan-setup="contacts" type="button" aria-haspopup="dialog" aria-controls="find-contacts-dialog" disabled>Find contacts</button>
      </div>
      <p class="scan-launch-lock" id="scan-launch-lock" role="status" hidden></p>
    </div>
    <dialog class="modal smart-scan-options-dialog" id="smart-scan-options-dialog" data-scan-mode="smart" aria-labelledby="smart-scan-options-title" aria-describedby="smart-scan-options-subtitle">
      <header class="modal-head">
        <div class="scan-setup-heading">
          <h2 id="smart-scan-options-title" tabindex="-1" autofocus>Smart Scan</h2>
          <p id="smart-scan-options-subtitle"></p>
        </div>
        <button type="button" class="icon-button" data-close-dialog="smart-scan-options-dialog" aria-label="Close scan setup">×</button>
      </header>
      <div class="modal-body scan-setup-body">
      <form id="scan-goal-form" class="scan-goal-form" data-verbatim>
        <label class="sr-only" for="scan-goal">Describe your scan goal</label>
        <div class="scan-goal-input">
          <svg class="scan-goal-sparkles" width="22" height="22" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
            stroke-linejoin="round" aria-hidden="true">
            <path d="m10 5 2 6 6 2-6 2-2 6-2-6-6-2 6-2 2-6Z"/>
            <path d="m19 2 1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3Z"/>
          </svg>
          <input id="scan-goal" name="goal" type="text" minlength="5" maxlength="500" required
            autocomplete="off" placeholder="Describe your goal in one sentence…"
            aria-describedby="scan-goal-status" />
          <button class="button primary" type="submit" aria-label="Configure scan with AI, $0.02 maximum"
            title="Set checks and conservative limits with AI · $0.02 max">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 19V5m-6 6 6-6 6 6"/>
            </svg>
            <span data-configuring-label hidden>Configuring…</span>
          </button>
        </div>
        <span id="scan-goal-status" role="status"></span>
      </form>
      <p id="scan-setup-lane-lock" class="scan-setup-availability" role="status" hidden>Settings are locked while a scan or estimate is in progress.</p>
      <form id="microlink-discovery-form" class="scan-option-panel" data-scan-setup-panel="discovery" hidden>
        <section class="scan-option-group">
          <h3>Search scope</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row">
              <label class="field scan-setup-scope"><span>Find companies by</span><select name="lane"><option value="mission">Selected mission and adjacent industries</option><option value="both">Creative hiring and launches</option><option value="hiring">Creative hiring</option><option value="launch">Launches</option></select></label>
              <p class="scan-option-detail">Searches the US, UK, Canada and Australia.</p>
            </div>
          </div>
        </section>
        <section class="scan-option-group">
          <h3>Company filters</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row"><label class="smart-scan-ai-toggle scan-option-toggle"><input name="prefer_large" type="checkbox" checked /><span>Prefer signs of 50+ employees or enterprise scale</span></label></div>
            <div class="scan-option-row"><label class="smart-scan-ai-toggle scan-option-toggle"><input name="limit_to_com" type="checkbox" /><span>Limit to .com</span></label></div>
            <div class="scan-option-row"><label class="smart-scan-ai-toggle scan-option-toggle"><input name="limit_to_ai" type="checkbox" /><span>Limit to .ai</span></label></div>
          </div>
        </section>
        <section class="scan-option-group">
          <h3>Search limits</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row scan-option-fields">
              <label class="smart-scan-ai-limit"><span>Max search requests</span><input name="request_limit" type="number" min="1" max="5000" value="1000" required /></label>
              <label class="smart-scan-ai-limit"><span>Max new companies</span><input name="candidate_limit" type="number" min="1" max="5000" value="1000" required /></label>
            </div>
          </div>
        </section>
      </form>
      <form id="microlink-contacts-form" class="scan-option-panel" data-scan-setup-panel="contacts" hidden>
        <section class="scan-option-group">
          <h3>Company list</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row">
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Top companies to check</span><input name="company_count" type="number" min="1" max="5000" value="25" required /></label>
              </div>
              <p class="scan-option-detail">Fills the limit with the next due companies in the selected mission, prioritizing evidence of 50+ employees or enterprise scale.</p>
            </div>
            <div class="scan-option-row"><label class="smart-scan-ai-toggle scan-option-toggle"><input name="force_contacts" type="checkbox" /><span>Include recently checked companies</span></label></div>
          </div>
        </section>
      </form>
      <button type="button" class="button scan-basic-option" id="scan-basic-option" aria-pressed="true">Basic</button>
      <div id="saved-scan-goals" data-scan-setup-panel="smart" aria-label="Saved scan goals"></div>
      <section class="scan-methods-disclosure" id="scan-methods-disclosure" data-scan-setup-panel="smart" aria-label="Scan modes">
        <div id="scan-methods-content"></div>
      </section>
      <div class="scan-option-panel" data-scan-setup-panel="smart">
        <p id="smart-scan-settings-lock" class="scan-setup-availability" role="status" hidden></p>
        <section class="scan-option-group" aria-labelledby="smart-scan-scope-heading">
          <h3 id="smart-scan-scope-heading">Scope &amp; filters</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row scan-discovery-row">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-due-hydration" type="checkbox" checked /><span>Discover and refresh companies</span></label>
                  ${scanCardHelp("discover", "Discover and refresh companies", "Find companies through public sources and refresh company websites that are due.")}
                </div>
                <small class="scan-option-detail">Public sources · 90-day refresh window</small>
              </div>
              <div class="scan-discovery-details">
                <label class="smart-scan-ai-limit"><span>Max company refreshes</span>
                  <input id="smart-scan-due-limit" type="number" min="1" max="10000"
                    inputmode="numeric" placeholder="All due" aria-label="Maximum company refreshes" />
                  <small>Source discovery is separate from this company limit.</small>
                </label>
                <div class="scan-option-checks">
                  <div class="scan-discovery-addon">
                    <label class="smart-scan-ai-toggle"><input id="smart-scan-use-source-ai" type="checkbox" /><span>Source research via OpenAI</span></label>
                    <small id="smart-scan-source-ai-estimate" class="scan-option-estimate" hidden></small>
                  </div>
                  <div class="scan-discovery-addon">
                    <label class="smart-scan-ai-toggle"><input id="smart-scan-use-microlink" type="checkbox" /><span>Website fallback via Microlink</span></label>
                    <small id="smart-scan-microlink-cost" class="scan-option-estimate" hidden></small>
                  </div>
                </div>
                <label class="field">
                  <span>Search terms <small>optional</small></span>
                  <textarea id="smart-scan-discovery-terms" rows="2" placeholder="Leave blank for automatic search" aria-describedby="smart-scan-terms-help"></textarea>
                </label>
                <small id="smart-scan-terms-help" class="scan-option-detail">5 lines max · 100 characters per term</small>
              </div>
            </div>
            <div class="scan-option-row">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-skip-failures" type="checkbox" /><span>Skip repeat failures</span></label>
                  ${scanCardHelp("failures", "Repeat failures", "Skip a scan method after at least three attempts in seven days if it meets your failure threshold.")}
                </div>
                <small class="scan-option-detail">3+ attempts per scan type in the last 7 days</small>
              </div>
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Minimum failure rate (%)</span><input id="smart-scan-failure-rate" type="number" inputmode="decimal" min="0" max="100" step="1" value="75" /></label>
              </div>
            </div>
          </div>
        </section>
        <section class="scan-option-group" aria-labelledby="smart-scan-paid-heading">
          <h3 id="smart-scan-paid-heading">Paid research</h3>
          <div class="scan-option-rows">
            <div class="scan-option-row smart-scan-ai-card">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-use-ai" type="checkbox" aria-describedby="smart-scan-ai-description" /><span>AI company review</span></label>
                  ${scanCardHelp("ai", "AI company review via OpenAI", "Use OpenAI to review top companies and fill important evidence gaps.")}
                </div>
                <small id="smart-scan-ai-description" class="scan-option-detail"><b>OpenAI</b> · Reviews top matches after other research</small>
                <small id="smart-scan-ai-estimate" class="scan-option-estimate" hidden></small>
              </div>
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Max AI reviews</span><input id="smart-scan-ai-sites" type="number" inputmode="numeric" min="1" max="100" step="1" value="100" /></label>
              </div>
            </div>
            <div class="scan-option-row smart-scan-seo-card">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-use-seo" type="checkbox" aria-describedby="smart-scan-seo-description" /><span>Search visibility</span></label>
                  ${scanCardHelp("seo", "Search visibility via DataForSEO", "Use DataForSEO to check Google search visibility for ranked company domains.")}
                </div>
                <small id="smart-scan-seo-description" class="scan-option-detail"><b>DataForSEO</b> · Checks Google rankings for top sites</small>
                <small id="smart-scan-seo-estimate" class="scan-option-estimate" hidden></small>
              </div>
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Max domains to check</span><input id="smart-scan-seo-sites" type="number" inputmode="numeric" min="1" max="100000" step="1" value="100" /></label>
              </div>
            </div>
            <div class="scan-option-row smart-scan-ads-card">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-use-ads" type="checkbox" aria-describedby="smart-scan-ads-description" /><span>Meta ad research</span></label>
                  ${scanCardHelp("ads", "Meta ad research via Apify", "Use Apify to check for company-linked Meta ads within your spend limit.")}
                </div>
                <small id="smart-scan-ads-description" class="scan-option-detail"><b>Apify</b> · Checks for company-linked Meta ads</small>
                <small id="smart-scan-ads-cost" class="scan-option-estimate" hidden></small>
              </div>
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Top companies</span><input id="smart-scan-ads-sites" type="number" inputmode="numeric" min="1" max="500" step="1" value="100" /></label>
                <label class="smart-scan-ai-limit"><span>Max spend ($)</span><input id="smart-scan-ads-cap" type="number" inputmode="decimal" min="0.10" max="100" step="0.01" value="5" /></label>
              </div>
            </div>
            <div class="scan-option-row smart-scan-people-card">
              <div class="scan-option-copy">
                <div class="scan-card-title">
                  <label class="smart-scan-ai-toggle scan-option-toggle"><input id="smart-scan-use-people" type="checkbox" aria-describedby="smart-scan-people-description" /><span>People lookup</span></label>
                  ${scanCardHelp("treg", "People lookup via Treg", "Use Treg to find relevant contacts at eligible ranked companies.")}
                </div>
                <small id="smart-scan-people-description" class="scan-option-detail"><b>Treg</b> · Finds decision-makers and reuses checks for 90 days</small>
                <small id="smart-scan-people-scope" class="scan-option-estimate" hidden></small>
              </div>
              <div class="scan-option-fields">
                <label class="smart-scan-ai-limit"><span>Top companies</span><input id="smart-scan-people-sites" type="number" inputmode="numeric" min="1" max="2500" step="1" value="100" /></label>
              </div>
            </div>
          </div>
          <p id="smart-scan-provider-note" class="scan-option-detail" hidden>Connect providers in Settings to add more research.</p>
        </section>
        ${microlinkHydrationMarkup()}
      </div>
      </div>
      <footer class="modal-actions scan-setup-footer">
        <p id="scan-setup-estimate-text" role="status" hidden></p>
        <div class="scan-setup-actions">
          <button class="button" type="button" data-close-dialog="smart-scan-options-dialog">Close</button>
          <button class="button primary" id="scan-setup-start" type="button" hidden disabled></button>
          <button class="button primary" id="start-smart-scan" type="button" disabled aria-live="polite">Scan</button>
        </div>
      </footer>
    </dialog>
      <div class="smart-scan-progress-column">
        <div class="smart-scan-phase-head"><span id="smart-scan-phase-label">Checking scan status…</span><strong id="smart-scan-percent">—</strong></div>
        <div class="smart-scan-track" role="progressbar" aria-label="Smart Scan progress" aria-valuemin="0" aria-valuemax="100" id="smart-scan-track"><span id="smart-scan-track-fill"></span></div>
        <div class="smart-scan-toolbar">
          <div class="smart-scan-toolbar-actions">
            <button class="button" id="clear-smart-scan" type="button" hidden>Clear scan</button>
          </div>
        </div>
        <ol class="smart-scan-phases" id="smart-scan-phases" aria-label="Scan steps" hidden></ol>
        <p class="smart-scan-note" id="smart-scan-note"></p>
      </div>
    </div>
    <div class="engine-pane-divider" role="separator" tabindex="0" aria-label="Resize Scan panes"
      aria-controls="smart-scan-controls" aria-orientation="vertical"></div>
    <div class="smart-scan-monitor">
      <section class="panel smart-scan-summary" aria-label="Smart Scan inventory" aria-busy="true">
        <div><span>Available targets</span><strong id="smart-scan-base">${countLoader}</strong><small id="smart-scan-base-note" hidden></small></div>
        <div><span>Need hydration</span><strong id="smart-scan-due">${countLoader}</strong><small id="smart-scan-due-note">Checking coverage…</small><button class="text-button" id="retry-scan-inventory" type="button" hidden>Retry inventory check</button></div>
      </section>
      <section class="smart-terminal" aria-label="Smart Scan activity">
        <header><span><i></i><span class="smart-terminal-tabs" role="tablist" aria-label="Smart Scan log"><button type="button" role="tab" id="smart-terminal-activity-tab" aria-selected="true" disabled>Activity</button><button type="button" role="tab" id="smart-terminal-errors-tab" aria-selected="false" disabled>Failures <b id="smart-terminal-error-count">—</b></button></span></span><span class="smart-terminal-stats"><button class="smart-terminal-copy" id="copy-smart-scan-errors" type="button" hidden>Copy</button><small id="smart-terminal-elapsed"></small><small id="smart-terminal-meta"></small><button class="smart-terminal-wrap" id="smart-terminal-wrap" type="button" aria-controls="smart-terminal-lines" aria-pressed="true" aria-label="Wrap lines" title="Wrapped lines · switch to single line" disabled><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path class="smart-terminal-wrap-on" d="M3 6h13a4 4 0 0 1 0 8h-5m3-3-3 3 3 3M3 12h4m-4 7h4"/><path class="smart-terminal-wrap-off" d="M3 6h18M3 18h18M3 12h18m-4-4 4 4-4 4"/></svg></button></span></header>
        <div class="smart-terminal-lines" id="smart-terminal-lines" role="log" aria-live="polite" aria-relevant="additions text"></div>
        <p class="smart-terminal-error-note" id="smart-terminal-error-note" hidden data-verbatim>Note: Some failures are expected when running large scraping tasks!</p>
      </section>
    </div>`;
}

function smartScanShellMarkup() {
  return `<section class="smart-scan-shell" id="smart-scan-shell" aria-label="Smart Scan" aria-busy="true" data-initial-loading="true" data-launch-status-pending="true" data-microlink-status-pending="true" data-overview-pending="true">
    ${smartScanContentMarkup()}
  </section>`;
}

function smartScanDirectoryProgress(scan = {}) {
  const source = scan.source_progress;
  if (scan.due_hydration_enabled === false || !source || source.total == null) return "";
  return `${formatNumber(source.completed || 0)} / ${formatNumber(source.total)} directories processed`;
}

function smartScanProgress(scan = {}) {
  const work = scan.work || {};
  const active = ["starting", "running", "paused", "reconnecting", "restarting"].includes(scan.status);
  if (active && Number(work.total) > 0 && scan.progress_basis !== "estimated_work") {
    // Older Oracle builds publish completed-phase percentages but already report work counts.
    return Math.max(0, Math.min(99, 100 * Number(work.completed || 0) / Number(work.total)));
  }
  const exact = Number(scan.progress_percent);
  if (Number.isFinite(exact)) return Math.max(0, Math.min(100, exact));
  const phases = Array.isArray(scan.phases) ? scan.phases : [];
  const phaseCount = Number(scan.phase_count || phases.length || 1);
  const phaseIndex = Math.max(1, Number(scan.phase_index || 1));
  if (["complete", "partial"].includes(String(scan.status || "").toLowerCase())) return 100;
  if (["failed", "interrupted"].includes(String(scan.status || "").toLowerCase())) return Math.min(99, (phaseIndex - 1) / phaseCount * 100);
  let local = .08;
  if (scan.phase === "source_execution") {
    const progress = scan.source_progress || {};
    local = Number(progress.total || 0) ? Number(progress.completed || 0) / Number(progress.total) : .08;
  }
  if (scan.phase === "base_hydration") {
    const progress = scan.hydration_progress || {};
    if (progress.stage === "complete") local = 1;
    else if (progress.stage === "scanners") {
      const scannerProgress = Number(progress.candidate_boundary || 0) ? Number(progress.scanner_cursor || 0) / Number(progress.candidate_boundary) : 0;
      local = .8 + .2 * scannerProgress;
    } else {
      const coreProgress = Number(progress.due_at_boundary || 0) ? Number(progress.submitted || 0) / Number(progress.due_at_boundary) : 0;
      local = .8 * coreProgress;
    }
  }
  return Math.max(1, Math.min(99, ((phaseIndex - 1) + Math.max(0, Math.min(1, local))) / phaseCount * 100));
}

function smartScanElapsedLabel(scan = {}, now = Date.now()) {
  const started = Date.parse(scan.started_at || "");
  if (!Number.isFinite(started)) return "";
  const finished = Date.parse(scan.finished_at || "");
  const end = Number.isFinite(finished) ? finished : now;
  const totalSeconds = Math.max(0, Math.floor((end - started) / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor(totalSeconds % 86400 / 3600);
  const minutes = Math.floor(totalSeconds % 3600 / 60);
  const seconds = totalSeconds % 60;
  const clock = `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${days ? `${days}d ` : ""}${clock}`;
}

function highlightLatestTerminalActivity(terminal, enabled = true) {
  let latest = null;
  let timestamp = -Infinity;
  if (enabled) {
    for (const row of terminal.children) {
      if (!row.matches(".smart-terminal-line:not(.level-error)")) continue;
      const time = Number(row.dataset.activityTime || 0);
      if (time < timestamp) continue;
      latest = row;
      timestamp = time;
    }
  }
  for (const previous of terminal.querySelectorAll(".is-current-activity")) {
    if (previous === latest) continue;
    previous.classList.remove("is-current-activity");
    const time = previous.querySelector("time[aria-label]");
    if (time) {
      time.textContent = time.dataset.eventTime;
      time.removeAttribute("aria-label");
    }
  }
  latest?.classList.add("is-current-activity");
}

function updateSmartScanActivityClock() {
  if (standaloneScanFocus) return;
  if (state.smartScanTerminalTab !== "activity" || !smartScanActive() || state.microlinkRunControl) return;
  const row = $("#smart-terminal-lines [data-live-action].is-current-activity");
  if (!row) return;
  const time = row.querySelector("time");
  if (!time) return;
  if (!time.dataset.eventTime) time.dataset.eventTime = time.textContent;
  const clock = new Intl.DateTimeFormat("en", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date());
  if (time.textContent !== clock) time.textContent = clock;
  time.setAttribute("aria-label", "Current time");
}

function updateSmartScanElapsed() {
  const elapsed = $("#smart-terminal-elapsed");
  if (elapsed) {
    const scan = standaloneScanFocus?.run || state.smartScan || {};
    const now = Date.now();
    const total = smartScanElapsedLabel(scan, now);
    const current = smartScanElapsedLabel({
      started_at: scan.phase_started_at,
      finished_at: scan.finished_at,
    }, now);
    elapsed.textContent = total ? `${total} total` : "";
    if (total && current) elapsed.textContent += ` • ${current} current`;
  }
  updateSmartScanActivityClock();
}

function startSmartScanElapsedTicker() {
  clearInterval(state.smartScanElapsedTimer);
  state.smartScanElapsedTimer = null;
  updateSmartScanElapsed();
  if (smartScanActive()) state.smartScanElapsedTimer = setInterval(updateSmartScanElapsed, 1000);
}

const terminalRowMarkup = new WeakMap();
const terminalLastInput = new WeakMap();

function smartScanTerminalNearBottom(terminal) {
  return performance.now() - (terminalLastInput.get(terminal) ?? -Infinity) > 350
    && terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight <= 2;
}

function renderTerminalRows(terminal, rows, {keepCurrent = false, follow = false} = {}) {
  const current = keepCurrent ? terminal.querySelector("[data-live-action]") : null;
  const existing = [...terminal.children].filter(node => node !== current);
  if (existing.length === rows.length
      && existing.every((node, index) => terminalRowMarkup.get(node) === rows[index])) return;
  const viewportTop = terminal.getBoundingClientRect().top;
  const retainedMarkup = new Set(rows);
  const anchor = !follow && existing.find(node => retainedMarkup.has(terminalRowMarkup.get(node))
    && node.getBoundingClientRect().bottom > viewportTop);
  const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
  const previousScroll = terminal.scrollTop;
  const available = new Map();
  for (const node of existing) {
    const markup = terminalRowMarkup.get(node);
    if (!available.has(markup)) available.set(markup, []);
    available.get(markup).push(node);
  }
  const template = document.createElement("template");
  const desired = rows.map(markup => {
    const reused = available.get(markup)?.shift();
    if (reused) return reused;
    template.innerHTML = markup;
    const node = template.content.firstElementChild;
    terminalRowMarkup.set(node, markup);
    return node;
  });
  for (const unused of available.values()) for (const node of unused) node.remove();
  let next = terminal.firstElementChild;
  for (const node of desired) {
    if (node === next) next = next.nextElementSibling;
    else terminal.insertBefore(node, next);
  }
  if (follow) terminal.scrollTop = terminal.scrollHeight;
  else if (anchor?.isConnected) {
    const shift = anchor.getBoundingClientRect().top - anchorTop;
    if (Math.abs(shift) >= 1) terminal.scrollTop += shift;
  } else if (terminal.scrollTop !== previousScroll) {
    terminal.scrollTop = previousScroll;
  }
}

function paintSmartScanCurrentAction(followOutput = null) {
  if (standaloneScanFocus) return;
  if (state.smartScanTerminalTab !== "activity" || !smartScanActive() || state.microlinkRunControl) return;
  const terminal = $("#smart-terminal-lines");
  if (!terminal) return;
  const shouldFollowOutput = followOutput === null ? smartScanTerminalNearBottom(terminal) : followOutput;
  let row = terminal.querySelector("[data-live-action]");
  if (!state.smartScanCurrentAction) {
    row?.remove();
    highlightLatestTerminalActivity(terminal);
    return;
  }
  if (!row) {
    terminal.insertAdjacentHTML("beforeend", smartScanTerminalLine({
      ...state.smartScanCurrentAction,
      level: "item",
    }));
    row = terminal.querySelector(".smart-terminal-line.level-item:last-child");
  }
  if (!row) return;
  row.dataset.liveAction = "";
  row.dataset.activityTime = String(Date.parse(state.smartScanCurrentAction.timestamp) || 0);
  highlightLatestTerminalActivity(terminal);
  updateSmartScanActivityClock();
  const message = row.querySelector("strong");
  const detail = row.querySelector("small");
  const text = state.smartScanCurrentAction.message || "Working";
  const detailText = state.smartScanCurrentAction.detail || "";
  if (message && message.textContent !== text) message.textContent = text;
  if (detail && detail.textContent !== detailText) detail.textContent = detailText;
  if (!detail && detailText) {
    row.querySelector("p")?.insertAdjacentHTML(
      "beforeend",
      `<small>${escapeHtml(state.smartScanCurrentAction.detail)}</small>`,
    );
  }
  if (shouldFollowOutput && terminal.scrollHeight - terminal.scrollTop > terminal.clientHeight + 1) {
    terminal.scrollTop = terminal.scrollHeight;
  }
}

function advanceSmartScanAction() {
  const next = state.smartScanActionQueue.shift();
  if (next) {
    state.smartScanCurrentAction = next;
    paintSmartScanCurrentAction();
  }
  if (!state.smartScanActionQueue.length && state.smartScanActionTimer) {
    clearInterval(state.smartScanActionTimer);
    state.smartScanActionTimer = null;
  }
}

function queueSmartScanActions(scan = {}) {
  const runId = scan.id || null;
  if (runId !== state.smartScanActionRunId) {
    clearInterval(state.smartScanActionTimer);
    state.smartScanActionTimer = null;
    state.smartScanActionRunId = runId;
    state.smartScanActionSeen = 0;
    state.smartScanActionQueue = [];
    state.smartScanCurrentAction = null;
  }
  if (!smartScanActive(scan)) {
    clearInterval(state.smartScanActionTimer);
    state.smartScanActionTimer = null;
    state.smartScanActionQueue = [];
    state.smartScanCurrentAction = null;
    return;
  }
  for (const action of Array.isArray(scan.actions) ? scan.actions : []) {
    const actionId = Number(action.id || 0);
    if (actionId <= state.smartScanActionSeen) continue;
    state.smartScanActionSeen = actionId;
    state.smartScanActionQueue.push(action);
  }
  if (state.smartScanActionQueue.length && !state.smartScanActionTimer) {
    advanceSmartScanAction();
    state.smartScanActionTimer = setInterval(advanceSmartScanAction, 100);
  }
}

function syncSmartScanLiveErrors(scan = {}) {
  const runId = scan.id || null;
  if (runId !== state.smartScanErrorRunId) {
    state.smartScanErrorGeneration = (state.smartScanErrorGeneration || 0) + 1;
    state.smartScanErrorsLoading = false;
    state.smartScanErrorRunId = runId;
    state.smartScanErrors = [];
    state.smartScanErrorOffset = 0;
    state.smartScanErrorTotal = 0;
    state.smartScanErrorsHasMore = false;
    state.smartScanLiveErrorSeen = new Set();
  }
  const revision = Number(scan.error_resolution_revision || 0);
  if (revision !== Number(state.smartScanErrorResolutionRevision || 0)) {
    state.smartScanErrorResolutionRevision = revision;
    state.smartScanErrorGeneration = (state.smartScanErrorGeneration || 0) + 1;
    state.smartScanErrorsLoading = false;
    state.smartScanErrors = [];
    state.smartScanErrorOffset = 0;
    state.smartScanLiveErrorSeen = new Set();
    const visibleActions = new Set((scan.actions || []).map(action => action.id));
    state.smartScanActionQueue = state.smartScanActionQueue.filter(action => visibleActions.has(action.id));
    if (state.smartScanCurrentAction && !visibleActions.has(state.smartScanCurrentAction.id)) {
      state.smartScanCurrentAction = null;
      paintSmartScanCurrentAction();
    }
    if (state.smartScanTerminalTab === "errors") void loadSmartScanErrors(true);
  }
  const liveErrors = (Array.isArray(scan.events) ? scan.events : [])
    .filter(event => event.level === "error");
  liveErrors.forEach(error => {
    const liveId = String(error.id ?? `${error.timestamp || ""}:${error.message || ""}:${error.detail || ""}`);
    if (state.smartScanLiveErrorSeen.has(liveId)) return;
    state.smartScanLiveErrorSeen.add(liveId);
    state.smartScanErrors.unshift({ ...error, live_event_id: liveId });
  });
  state.smartScanErrorTotal = Number(scan.error_total || 0);
  if (!state.smartScanErrorOffset && state.smartScanErrorTotal > state.smartScanErrors.length) {
    state.smartScanErrorsHasMore = true;
  }
}

function smartScanCachedCounts(scan) {
  const scope = scan.inventory_cache_scope;
  if (!scope || scan.preparing || scan.running) return null;
  const key = "beings-growth:scan-counts:v1";
  try {
    const counts = scan.inventory_pending
      ? JSON.parse(localStorage.getItem(key) || "null")
      : {
          scope,
          checkedAt: scan.inventory_as_of,
          due_candidates: scan.inventory?.due_candidates,
          eligible_candidates: scan.inventory?.eligible_candidates,
        };
    if (counts?.scope !== scope || !Number.isFinite(Date.parse(counts.checkedAt))
        || !Number.isSafeInteger(counts.due_candidates) || counts.due_candidates < 0
        || !Number.isSafeInteger(counts.eligible_candidates)
        || counts.eligible_candidates < counts.due_candidates) return null;
    if (scan.inventory_pending) return counts;
    if (!scan.inventory_error) localStorage.setItem(key, JSON.stringify(counts));
  } catch {
    // Private browsing, storage limits and bad saved data never block the check.
  }
  return null;
}

function renderSmartScan(scan = {}) {
  state.smartScan = scan;
  queueSmartScanActions(scan);
  syncSmartScanLiveErrors(scan);
  const root = $("#smart-scan-shell");
  if (!root) return;
  if (root.dataset.initialLoading === "true") {
    delete root.dataset.initialLoading;
    root.querySelectorAll(".smart-scan-head button, .smart-terminal-tabs button, #smart-terminal-wrap")
      .forEach(button => { button.disabled = false; });
    bindScanLauncher();
    const phaseList = $("#smart-scan-phases", root);
    phaseList.addEventListener("click", event => {
      const button = event.target.closest("[data-phase-group]");
      if (!button) return;
      const group = button.dataset.phaseGroup;
      const expanded = !button.hasAttribute("data-collapse");
      phaseList.dataset[group] = String(expanded);
      phaseList.querySelector(`[data-phase-group="${group}"]${expanded ? "[data-collapse]" : ":not([data-collapse])"}`)
        ?.focus({preventScroll:true});
    });
    bindScanSetup(root, bindMicrolinkScanBar(root));
    root.dispatchEvent(new Event("microlink-refresh"));
    $("#smart-scan-discovery-terms", root).addEventListener("input", event => {
      const field = event.currentTarget;
      state.smartScanSearchTermsDraft = field.value;
      field.setCustomValidity("");
    });
    $("#smart-scan-due-limit", root).addEventListener("input", event => {
      state.smartScanDueCompanyLimit = event.target.value ? Number(event.target.value) : null;
    });
    $("#smart-scan-due-hydration", root).addEventListener("change", event => {
      state.smartScanDueHydrationEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-use-microlink", root).addEventListener("change", event => {
      state.smartScanMicrolinkEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    root.querySelectorAll("[data-microlink-option]").forEach(control => {
      control.addEventListener("change", event => {
        const options = microlinkHydrationOptions();
        options[event.currentTarget.dataset.microlinkOption] = event.currentTarget.checked;
        if (!options.contacts) options.browser_interactions = false;
        state.smartScanMicrolinkHydration = options;
        updateSmartScanLaunchControls();
      });
    });
    for (const [id, key] of [["companies", "company_limit"], ["requests", "request_limit"]]) {
      $(`#smart-scan-ml-${id}`, root).addEventListener("input", event => {
        state.smartScanMicrolinkHydration = {
          ...microlinkHydrationOptions(), [key]: Number(event.currentTarget.value),
        };
        updateSmartScanLaunchControls();
      });
    }
    $("#smart-scan-use-source-ai", root).addEventListener("change", event => {
      state.smartScanSourceAIEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-use-ads", root).addEventListener("change", event => {
      state.smartScanAdsEnabled = event.target.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-ads-sites", root).addEventListener("input", event => {
      state.smartScanAdsCompanyLimit = Number(event.target.value);
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-ads-cap", root).addEventListener("input", event => {
      state.smartScanAdsMaxUSD = Number(event.target.value);
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-use-people", root).addEventListener("change", event => {
      state.smartScanPeopleEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-people-sites", root).addEventListener("input", event => {
      state.smartScanPeopleCompanyLimit = Number(event.currentTarget.value);
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-use-ai", root).addEventListener("change", event => {
      state.smartScanAIEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-ai-sites", root).addEventListener("input", event => {
      const value = Number(event.currentTarget.value);
      if (Number.isFinite(value)) state.smartScanAISiteLimit = value;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-use-seo", root).addEventListener("change", event => {
      state.smartScanSEOEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-seo-sites", root).addEventListener("input", event => {
      const value = Number(event.currentTarget.value);
      if (Number.isFinite(value)) state.smartScanSEOSiteLimit = value;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-skip-failures", root).addEventListener("change", event => {
      state.smartScanFailureSkipEnabled = event.currentTarget.checked;
      updateSmartScanLaunchControls();
    });
    $("#smart-scan-failure-rate", root).addEventListener("input", event => {
      const value = Number(event.currentTarget.value);
      if (Number.isFinite(value)) state.smartScanFailureRate = value;
      updateSmartScanLaunchControls();
    });
    $("#pause-resume-smart-scan", root).addEventListener("click", event => {
      const action = event.currentTarget.value;
      if (state.microlinkRunControl) void state.microlinkRunControl(action);
      else void controlSmartScan(action);
    });
    $("#stop-smart-scan", root).addEventListener("click", () => state.microlinkRunControl ? state.microlinkRunControl("stop") : controlSmartScan("stop"));
    $("#clear-smart-scan", root).addEventListener("click", clearSmartScan);
    const terminal = $("#smart-terminal-lines", root);
    for (const type of ["wheel", "touchmove", "pointerdown", "keydown"]) {
      terminal.addEventListener(type, () => terminalLastInput.set(terminal, performance.now()), {passive:true});
    }
    terminal.addEventListener("click", event => {
      if (event.target.closest("#smart-terminal-more-errors")) void loadSmartScanErrors(false);
    });
    $("#smart-terminal-activity-tab", root).addEventListener("click", () => selectSmartScanTerminalTab("activity"));
    $("#smart-terminal-errors-tab", root).addEventListener("click", () => selectSmartScanTerminalTab("errors"));
    $("#copy-smart-scan-errors", root).addEventListener("click", copySmartScanErrors);
    try { state.smartScanTerminalWrap = localStorage.getItem("beings.scan.terminal.wrap") !== "false"; } catch {}
    const wrapButton = $("#smart-terminal-wrap", root);
    const applyWrap = () => {
      $("#smart-terminal-lines", root).classList.toggle("is-nowrap", !state.smartScanTerminalWrap);
      wrapButton.setAttribute("aria-pressed", String(state.smartScanTerminalWrap));
      wrapButton.title = state.smartScanTerminalWrap
        ? "Wrapped lines · switch to single line" : "Single line · wrap lines";
    };
    wrapButton.addEventListener("click", () => {
      state.smartScanTerminalWrap = !state.smartScanTerminalWrap;
      try { localStorage.setItem("beings.scan.terminal.wrap", String(state.smartScanTerminalWrap)); } catch {}
      applyWrap();
    });
    applyWrap();
  }
  root.setAttribute("aria-busy", String(Boolean(state.smartScanLoading)));
  $(".smart-scan-summary", root)?.setAttribute("aria-busy", String(Boolean(scan.inventory_pending && !scan.inventory_error)));
  const inventory = scan.inventory || {};
  const profile = scan.profile || {};
  const active = smartScanActive(scan);
  const status = String(scan.status || "idle").toLowerCase();
  const scanFinished = ["complete", "partial"].includes(status);
  const completionMessage = "Smart Scan completed";
  const initializing = smartScanInitializing(scan);
  const phases = Array.isArray(scan.phases) && scan.phases.length ? scan.phases
    : initializing ? [{id: "planning", label: "Quantify backlog"}] : [];
  const progress = smartScanProgress(scan);
  const counters = scan.counters || {};
  root.dataset.status = status;
  startSmartScanElapsedTicker();
  const savedCounts = smartScanCachedCounts(scan);
  const displayCounts = scan.inventory_pending ? savedCounts : inventory;
  const countLoader = scan.inventory_pending && !scan.inventory_error
    ? loadingIndicator("Checking available companies…", {iconOnly:true}) : "";
  for (const [selector, field] of [
    ["#smart-scan-due", "due_candidates"], ["#smart-scan-base", "eligible_candidates"],
  ]) {
    const counter = $(selector);
    const count = displayCounts?.[field];
    if (Number.isSafeInteger(count)) counter.textContent = formatNumber(count);
    else if (counter.innerHTML !== countLoader) counter.innerHTML = countLoader;
  }
  let inventoryNote = scan.inventory_progress || "Updating inventory…";
  if (scan.inventory_error) inventoryNote = "Inventory unavailable";
  if (!scan.inventory_pending) {
    inventoryNote = `${formatNumber(inventory.retry_deferred_candidates)} retry tomorrow`;
  } else if (savedCounts) {
    const checkedAt = new Date(savedCounts.checkedAt).toLocaleString(undefined, {
      dateStyle: "medium", timeStyle: "short",
    });
    inventoryNote = `Last checked ${checkedAt} · ${scan.inventory_error ? "Refresh unavailable" : "Updating…"}`;
  }
  $("#smart-scan-due-note").textContent = inventoryNote;
  const targetNote = $("#smart-scan-base-note");
  targetNote.hidden = scan.inventory_pending;
  targetNote.textContent = scan.inventory_pending ? "" : `${formatNumber(inventory.manually_disabled_candidates)} manually off · ${formatNumber(inventory.failure_rate_skipped_candidates)} rate-skipped`;
  const isTerminal = ["complete", "partial", "failed", "stopped"].includes(status);
  const clearButton = $("#clear-smart-scan");
  clearButton.hidden = !isTerminal || scan.cleaning_up === true;
  const clearHost = isTerminal ? $(".smart-scan-phase-head") : $(".smart-scan-toolbar-actions");
  if (clearButton.parentElement !== clearHost) clearHost.append(clearButton);
  clearButton.disabled = state.smartScanControlPending || state.smartScanStarting || smartScanBusy(scan) || Boolean(scan.external_source_runs?.length);
  if (!state.smartScanControlsInitialized) {
    state.smartScanDueHydrationEnabled = !active || scan.due_hydration_enabled !== false;
    state.smartScanDueCompanyLimit = active ? scan.due_company_limit ?? null : null;
    state.smartScanAdsEnabled = Boolean(active && scan.paid_ads_enabled);
    state.smartScanAdsCompanyLimit = active ? Number(scan.paid_ads_company_limit || 100) : 100;
    state.smartScanAdsMaxUSD = active ? Number(scan.paid_ads_max_usd || 5) : 5;
    state.smartScanPeopleEnabled = Boolean(active && scan.people_hydration_enabled);
    state.smartScanPeopleCompanyLimit = active ? Number(scan.people_company_limit || 2500) : 100;
    state.smartScanAIEnabled = Boolean(active && scan.ai_enabled);
    state.smartScanSourceAIEnabled = Boolean(active && (scan.source_ai_enabled ?? scan.ai_enabled));
    state.smartScanMicrolinkEnabled = Boolean(active && scan.microlink_enabled);
    state.smartScanMicrolinkHydration = active
      ? {...(scan.microlink_hydration || {})} : {company_limit:100, request_limit:500};
    state.smartScanAISiteLimit = active ? Number(scan.ai_site_limit || profile.candidate_ai_call_cap || 100) : 100;
    state.smartScanSEOEnabled = Boolean(active && scan.dataforseo_enabled);
    state.smartScanSEOSiteLimit = active ? Number(scan.seo_site_limit || counters.dataforseo_target_cap || profile.dataforseo_target_default || 100) : 100;
    state.smartScanFailureSkipEnabled = scan.max_company_failure_rate != null;
    state.smartScanFailureRate = Number(scan.max_company_failure_rate ?? 75);
    state.smartScanControlsInitialized = true;
  }
  const sourceActive = Boolean(scan.external_source_runs?.length);
  updateSmartScanLaunchControls(scan);
  const controlStatus = String(scan.control_status || "").toLowerCase();
  const controls = $("#smart-scan-run-controls");
  controls.hidden = Boolean(scan.cleaning_up || (!initializing && !active && !sourceActive));
  const paused = !initializing && (controlStatus === "paused" || status === "paused");
  const pauseResume = $("#pause-resume-smart-scan");
  pauseResume.value = paused ? "resume" : "pause";
  pauseResume.textContent = paused ? "Resume" : "Pause";
  pauseResume.hidden = !initializing && !active && !paused;
  pauseResume.disabled = initializing || state.smartScanControlPending || status === "pausing" || status === "stopping";
  const stopButton = $("#stop-smart-scan");
  stopButton.hidden = scan.cleaning_up === true;
  stopButton.disabled = Boolean(initializing || scan.cleaning_up || state.smartScanControlPending || status === "stopping");
  const directoryProgress = smartScanDirectoryProgress(scan);
  $("#smart-scan-phase-label").textContent = scanFinished ? completionMessage
    : [scan.message || "Ready to scan", scan.phase === "source_execution" ? directoryProgress : ""].filter(Boolean).join(" · ");
  const percentText = `${Number(progress.toFixed(2))}%`;
  const progressLabel = Number(scan.work?.total) > 0
    ? "Estimated share of planned work attempted; the plan can change as work is discovered"
    : "Scan progress";
  $("#smart-scan-percent").textContent = percentText;
  $("#smart-scan-track-fill").style.width = `${progress}%`;
  $("#smart-scan-track").setAttribute("aria-valuenow", String(progress));
  $("#smart-scan-track").setAttribute("aria-valuetext", `${percentText} · ${progressLabel}`);
  $("#smart-scan-track").setAttribute("title", progressLabel);
  const currentIndex = Number(scan.phase_index || (initializing ? 1 : 0));
  const idle = status === "idle";
  const dueHydration = idle ? state.smartScanDueHydrationEnabled : scan.due_hydration_enabled;
  const microlinkEnabled = idle
    ? Boolean(scan.profile?.microlink_hydration_available && selectedMicrolinkHydration())
    : Object.entries(scan.microlink_hydration || {}).some(([key, value]) => key !== "browser_interactions" && value === true);
  const optionalSteps = {
    microlink_hydration: microlinkEnabled,
    seo_correction: idle ? state.smartScanSEOEnabled : scan.dataforseo_enabled,
    paid_ads: idle ? state.smartScanAdsEnabled : Boolean(scan.paid_ads_enabled),
    ai_shortlist: idle ? state.smartScanAIEnabled : scan.ai_enabled,
    people_hydration: idle ? state.smartScanPeopleEnabled : scan.people_hydration_enabled,
  };
  const steps = phases.map((phase, index) => {
    const number = index + 1;
    const off = optionalSteps[phase.id] === false
      || (dueHydration === false && ["source_research", "source_execution", "base_hydration"].includes(phase.id));
    const finished = scanFinished || number < currentIndex;
    const current = number === currentIndex;
    return {phase, number, off, finished, current};
  });
  const enabledSteps = steps.filter(step => !step.off);
  const currentSteps = enabledSteps.filter(step => step.current && !step.finished);
  const focus = currentSteps[0] ?? enabledSteps.find(step => !step.finished) ?? enabledSteps.at(-1);
  const firstNeighbor = enabledSteps.findLast(step => step.number < focus?.number)?.number ?? focus?.number ?? 1;
  const lastCurrent = currentSteps.at(-1) ?? focus;
  const lastNeighbor = enabledSteps.find(step => step.number > lastCurrent?.number)?.number ?? lastCurrent?.number ?? 0;
  const fullTimeline = scanFinished || idle;
  const pastCount = fullTimeline ? 0 : enabledSteps.filter(step => step.number < firstNeighbor).length;
  const upcomingCount = fullTimeline ? 0 : enabledSteps.filter(step => step.number > lastNeighbor).length;
  const first = pastCount > 1 ? firstNeighbor : (enabledSteps[0]?.number ?? 1);
  const last = upcomingCount > 1 ? lastNeighbor : (enabledSteps.at(-1)?.number ?? 0);
  const phaseList = $("#smart-scan-phases");
  phaseList.hidden = enabledSteps.length === 0;
  phaseList.dataset.finished = String(scanFinished);
  const phaseGroup = (group, count, state, glyph) => count > 1 ? `<li class="${state} phase-group"><i aria-hidden="true">${glyph}</i><button type="button" data-phase-group="${group}" aria-expanded="false"><strong>${scanFinished && group === "past" ? `View completed steps · ${count}` : `+${count} steps`}</strong><span class="sr-only"> ${group === "past" ? "completed" : "upcoming"}</span></button></li>` : "";
  const phaseMarkup = phaseGroup("past", pastCount, "complete", "✓") + enabledSteps.map(({phase, number, off, finished, current}) => {
    const group = number < first ? " phase-past" : number > last ? " phase-upcoming" : "";
    const phaseState = off ? "pending" : finished ? "complete" : current ? "active" : "pending";
    const label = off ? "Off" : finished ? "Finished" : current && ["paused", "stopped", "pausing"].includes(status) ? humanize(status) : current && status === "failed" ? "Stopped" : current && initializing ? "Preparing" : current && active ? "Scanning" : "Waiting";
    const glyph = off ? "−" : finished ? "✓" : current ? "●" : "○";
    const pulsing = label === "Scanning" || label === "Preparing";
    const collapseGroup = number === enabledSteps[0]?.number && number < first ? "past"
      : number === enabledSteps.at(-1)?.number && number > last ? "upcoming" : "";
    const collapse = collapseGroup ? `<button class="icon-button phase-collapse" type="button" data-phase-group="${collapseGroup}" data-collapse aria-expanded="true" aria-label="Collapse ${collapseGroup === "past" ? "completed" : "upcoming"} steps" title="Collapse steps">⌃</button>` : "";
    return `<li class="${phaseState}${pulsing ? " is-pulsing" : ""}${group}"><i>${glyph}</i><span><strong>${escapeHtml(phase.label)}</strong><small>${escapeHtml([label, phase.id === "source_execution" && !off ? directoryProgress : ""].filter(Boolean).join(" · "))}</small></span>${collapse}</li>`;
  }).join("") + phaseGroup("upcoming", upcomingCount, "pending", "○");
  // Keep the orb in place between status polls when the timeline is unchanged.
  if (phaseList.dataset.renderedMarkup !== phaseMarkup) {
    phaseList.innerHTML = phaseMarkup;
    phaseList.dataset.renderedMarkup = phaseMarkup;
  }
  const work = scan.work || {};
  const attemptedWork = Number(work.completed || 0) + Number(work.in_flight || 0);
  const plannedWork = Math.max(Number(work.total || 0), attemptedWork);
  $("#smart-scan-note").textContent = scan.preparing
    ? "Preparing the exact scan plan…"
    : active
      ? `${formatNumber(attemptedWork)} / ${formatNumber(plannedWork)} planned items attempted · ${formatNumber(counters.hydration_hosts_submitted || 0)} due records submitted · ${formatNumber(counters.scanner_hosts_submitted || 0)} scanner-gap records.`
    : scan.inventory_pending ? scan.inventory_error ? "Inventory unavailable" : scan.inventory_progress || "Updating inventory…"
    : "";
  if (!root.dispatchEvent(new Event("microlink-render", {cancelable:true}))) return;
  $("#smart-terminal-error-count").textContent = formatNumber(scan.error_total || 0);
  $("#smart-terminal-activity-tab").setAttribute("aria-selected", String(state.smartScanTerminalTab === "activity"));
  $("#smart-terminal-errors-tab").setAttribute("aria-selected", String(state.smartScanTerminalTab === "errors"));
  const copyErrors = $("#copy-smart-scan-errors");
  copyErrors.hidden = state.smartScanTerminalTab !== "errors";
  $("#smart-terminal-error-note").hidden = copyErrors.hidden;
  copyErrors.disabled = !state.smartScanErrors.length;
  const events = (Array.isArray(scan.events) ? scan.events : [])
    .filter(event => !active || event.replace_key !== "current-child-action")
    .slice(-Number(profile.browser_log_limit || 80))
    .map(event => scanFinished && event.phase === "complete" && event.message === scan.message
      ? { ...event, message: completionMessage, level: "success", detail: "",
        results: smartScanCompletionResults(scan) }
      : event);
  const terminal = $("#smart-terminal-lines");
  const followOutput = smartScanTerminalNearBottom(terminal);
  const meta = $("#smart-terminal-meta");
  if (state.smartScanTerminalTab === "errors") {
    const rows = state.smartScanErrors.length
      ? state.smartScanErrors.map(error => smartScanTerminalLine({ ...error, level: "error" }))
      : ['<div class="smart-terminal-empty"><span>✓</span> No failures recorded for this Smart Scan.</div>'];
    if (state.smartScanErrorsHasMore) rows.push('<button class="smart-terminal-more" id="smart-terminal-more-errors" type="button">Load older failures</button>');
    renderTerminalRows(terminal, rows);
    const text = `${formatNumber(state.smartScanErrors.length)} / ${formatNumber(state.smartScanErrorTotal)} failures`;
    if (state.smartScanErrorsLoading) {
      if (!meta.querySelector(".loading-indicator")) meta.innerHTML = loadingIndicator("Loading failures…");
    } else if (meta.textContent !== text || meta.firstElementChild) meta.textContent = text;
  } else {
    const rows = events.length || active ? events.map(smartScanTerminalLine)
      : ['<div class="smart-terminal-empty"><span>›</span> Ready to scan.</div>'];
    renderTerminalRows(terminal, rows, {keepCurrent:active, follow:followOutput});
    if (active) paintSmartScanCurrentAction(followOutput);
    else highlightLatestTerminalActivity(terminal, false);
    if (meta.textContent) meta.textContent = "";
  }
  renderAggregateScanState();
}

function smartScanCompletionResults(scan) {
  const counters = scan.counters || {};
  const inventory = scan.inventory || {};
  const groups = [
    ["This scan", [
      [scan.new_companies_added, "company records added"],
      [counters.hydration_network_scanned, "website checks recorded"],
      [counters.hydration_reused, "fresh profiles reused"],
      [counters.scanner_executions_completed, "follow-up checks completed"],
    ]],
    ["Gaps", [
      [scan.error_total, "unresolved failures"],
      [counters.hydration_hosts_unrecovered || undefined, "records in refresh batches that failed again"],
    ]],
  ];
  if (!scan.inventory_pending && !scan.inventory_error) {
    groups.push(["Inventory now", [
      [inventory.due_candidates, "records eligible for research"],
      [inventory.retry_deferred_candidates, "records on retry hold"],
    ]]);
  }
  return groups.flatMap(([label, metrics]) => {
    const summary = metrics.filter(([count]) => Number.isSafeInteger(count))
      .map(([count, unit]) => `${formatNumber(count)} ${unit}`).join(" · ");
    return summary ? [{label, summary}] : [];
  });
}

function smartScanTerminalLine(event = {}) {
  const timestamp = Date.parse(event.timestamp) || 0;
  const time = timestamp ? new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(timestamp) : "--:--:--";
  const glyph = event.level === "error" ? "!" : event.level === "success" ? "✓" : event.level === "item" ? "↳" : "›";
  const results = (event.results || []).map(({label, summary}) =>
    `<small class="smart-terminal-result"><b>${escapeHtml(label)}</b> · ${escapeHtml(summary)}</small>`).join("");
  return `<div class="smart-terminal-line level-${escapeHtml(event.level || "info")}" data-activity-time="${timestamp}"><time>${escapeHtml(time)}</time><span class="terminal-glyph">${glyph}</span><p><strong>${escapeHtml(roundDollarText(event.message || "Update"))}</strong>${event.detail ? `<small>${escapeHtml(roundDollarText(event.detail))}</small>` : ""}${event.query ? `<small data-verbatim>${escapeHtml(event.query)}</small>` : ""}${results}</p></div>`;
}

async function copySmartScanErrors() {
  const errors = state.microlinkVisibleErrors ?? state.smartScanErrors;
  if (!errors.length) return showToast("No failures to copy", "info");
  const text = errors.map(error => [
    error.timestamp || "",
    error.message || "Failure",
    error.detail || "",
    error.query || "",
  ].filter(Boolean).join(" | ")).join("\n");
  await copyText(text);
  showToast(`${formatNumber(errors.length)} failures copied`);
}

async function selectSmartScanTerminalTab(tab) {
  state.smartScanTerminalTab = tab === "errors" ? "errors" : "activity";
  renderSmartScan(state.smartScan || {});
  if (state.smartScanTerminalTab === "errors" && !state.microlinkRunControl && !standaloneScanFocus) await loadSmartScanErrors(true);
}

async function loadSmartScanErrors(reset = false) {
  if (state.smartScanErrorsLoading || !state.smartScan?.id) return;
  const runId = state.smartScan.id;
  const generation = state.smartScanErrorGeneration;
  const current = () => state.smartScan?.id === runId && state.smartScanErrorGeneration === generation;
  const requestedOffset = reset ? 0 : state.smartScanErrorOffset;
  state.smartScanErrorsLoading = true;
  if (state.smartScanTerminalTab === "errors" && $("#smart-terminal-meta")) {
    $("#smart-terminal-meta").innerHTML = loadingIndicator("Loading failures…");
  }
  try {
    const data = await api(`/api/smart-scan/errors?offset=${requestedOffset}&limit=100`);
    if (!current()) return;
    const items = data.items || [];
    state.smartScanErrors = reset
      ? [...items]
      : [...state.smartScanErrors, ...items];
    state.smartScanErrorOffset = Number(data.offset || requestedOffset) + items.length;
    state.smartScanErrorTotal = Math.max(Number(data.total || 0), state.smartScanErrors.length);
    state.smartScanErrorsHasMore = Boolean(data.has_more);
    if (state.smartScan) state.smartScan.error_total = Number(data.operational_total ?? data.total ?? 0);
  } catch (error) {
    if (current()) showToast(error.message, "error");
  } finally {
    if (current()) {
      state.smartScanErrorsLoading = false;
      if (state.smartScanTerminalTab === "errors") renderSmartScan(state.smartScan || {});
    }
  }
}

async function controlSmartScan(action) {
  const previous = state.smartScan || {};
  const paused = previous.status === "paused" || previous.control_status === "paused";
  if (smartScanInitializing(previous) || state.smartScanControlPending || previous.cleaning_up || (!smartScanActive(previous) && !(action === "stop" && previous.external_source_runs?.length)) || previous.status === "stopping"
      || !["pause", "resume", "stop"].includes(action)
      || (action === "resume" ? !paused : action === "pause" && paused)) return;
  state.smartScanControlPending = true;
  const optimistic = action === "pause"
    ? { status: "paused", control_status: "paused", message: "Smart Scan paused; completed work is preserved" }
    : action === "resume"
      ? { status: "running", control_status: "running", message: "Smart Scan resuming" }
      : { status: "stopping", control_status: "stopping", message: "Stopping Smart Scan…" };
  renderSmartScan({ ...previous, ...optimistic });
  try {
    const scan = await api(`/api/smart-scan/${action}`, { method: "POST" });
    renderSmartScan(scan);
    showToast(`Smart Scan ${action === "pause" ? "pause requested" : action === "resume" ? "resumed" : "stopped"}`);
    if (smartScanBusy(scan)) pollSmartScan();
    else if (action === "stop") void pollScanStatus();
  } catch (error) {
    renderSmartScan(previous);
    showToast(error.message, "error");
  } finally {
    state.smartScanControlPending = false;
    renderSmartScan(state.smartScan || {});
    if (state.smartScan?.inventory_pending && !smartScanBusy()) {
      void refreshSmartScanInventory(state.smartScan, smartScanLoadGeneration);
    }
  }
}

async function clearSmartScan() {
  const previous = state.smartScan;
  if (!previous || smartScanBusy(previous)
      || state.smartScanControlPending || state.smartScanStarting
      || !["complete", "partial", "failed", "stopped"].includes(previous.status)) return;
  state.smartScanControlPending = true;
  const shell = $("#smart-scan-shell");
  if (shell) delete shell.dataset.scanStatusUnavailable;
  renderScanLauncherStatus();
  const generation = ++smartScanLoadGeneration;
  clearTimeout(state.smartScanPollTimer);
  smartScanInventoryRequest = null;
  renderSmartScan(previous);
  try {
    const scan = await api("/api/smart-scan/clear", { method: "POST" });
    state.smartScanTerminalTab = "activity";
    renderSmartScan(scan);
    void refreshSmartScanInventory(scan, generation);
  } catch (error) {
    renderSmartScan(previous);
    showToast(error.message, "error");
    if (previous.inventory_pending) void refreshSmartScanInventory(previous, generation);
  } finally {
    state.smartScanControlPending = false;
    updateSmartScanLaunchControls();
    const clearButton = $("#clear-smart-scan");
    if (clearButton) clearButton.disabled = state.smartScan?.running === true;
  }
}

let smartScanLoadGeneration = 0;
let smartScanInventoryRequest = null;

async function refreshSmartScanInventory(scan, generation) {
  // Yield a paint before the expensive compatibility inventory request.
  await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
  const current = () => generation === smartScanLoadGeneration
    && state.activeView === "smart_scan" && state.smartScan === scan && !state.smartScanStarting;
  if (!current()) {
    if (state.activeView !== "smart_scan") state.initializedViews.delete("smart_scan");
    return;
  }
  const key = JSON.stringify([scan.id, scan.status, scan.updated_at, generation]);
  if (smartScanInventoryRequest?.key === key) return;
  const request = {key};
  smartScanInventoryRequest = request;
  try {
    let checkId = scan.inventory_check_id;
    while (current()) {
      const suffix = checkId ? `?check_id=${encodeURIComponent(checkId)}` : "";
      const refreshed = await api(`/api/smart-scan/inventory${suffix}`, {
        signal: AbortSignal.timeout(10000),
      });
      if (!current()) {
        if (state.activeView !== "smart_scan") state.initializedViews.delete("smart_scan");
        return;
      }
      if (refreshed.id !== scan.id || refreshed.status !== scan.status) {
        void loadSmartScan();
        return;
      }
      renderSmartScan(refreshed);
      scan = refreshed;
      checkId = refreshed.inventory_check_id;
      if (!refreshed.inventory_pending || refreshed.inventory_error) return;
      // Poll the same owned read; a slow recount is never restarted by polling.
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (error) {
    if (current()) {
      renderSmartScan({ ...scan, inventory_error: true, inventory_transport_error: true });
      showToast(error.name === "TimeoutError"
        ? "Inventory check timed out. Use Retry inventory check; saved work is preserved."
        : error.message, "error");
    }
    state.initializedViews.delete("smart_scan");
  } finally {
    if (smartScanInventoryRequest === request) smartScanInventoryRequest = null;
  }
}

async function loadSmartScan() {
  state.smartScanPaidEstimate = null;
  state.smartScanPreviousPaidEstimate = null;
  state.smartScanPaidEstimateKey = "";
  state.smartScanPaidEstimateError = "";
  const shell = $("#smart-scan-shell");
  if (!shell) return;
  shell.dispatchEvent(new Event("microlink-refresh"));
  const generation = ++smartScanLoadGeneration;
  state.smartScanLoading = true;
  if (shell.dataset.initialLoading !== "true") updateSmartScanLaunchControls();
  try {
    const scan = await api("/api/smart-scan?fast=true");
    if (generation !== smartScanLoadGeneration || state.activeView !== "smart_scan") return;
    const previous = state.smartScan;
    if (previous?.inventory_transport_error && previous.id === scan.id
        && previous.status === scan.status && previous.updated_at === scan.updated_at) {
      scan.inventory_check_id = previous.inventory_check_id;
    }
    renderSmartScan(scan);
    if (smartScanBusy(scan)) pollSmartScan();
    if (scan.inventory_pending) void refreshSmartScanInventory(scan, generation);
  } catch (error) {
    if (generation === smartScanLoadGeneration && state.activeView === "smart_scan") {
      if (state.smartScan) {
        renderSmartScan({ ...state.smartScan, inventory_pending: true, inventory_error: true });
      }
      clearLoadingIndicators("#smart-scan-shell");
      $("#smart-scan-shell")?.setAttribute("aria-busy", "false");
      if (shell.dataset.initialLoading === "true") {
        shell.dataset.scanStatusUnavailable = "true";
        renderScanLauncherStatus();
        $(".smart-scan-summary", shell)?.setAttribute("aria-busy", "false");
        $("#smart-scan-phase-label", shell).textContent = "Scan status unavailable";
        $("#smart-scan-due-note", shell).textContent = "Inventory unavailable";
      }
      showToast(error.message, "error");
    }
  } finally {
    if (generation === smartScanLoadGeneration) {
      state.smartScanLoading = false;
      if (shell.dataset.initialLoading !== "true") updateSmartScanLaunchControls();
    }
  }
}

async function startSmartScan() {
  if (smartScanLaunchBlocked() || scanActionBlocked($("#start-smart-scan"))) return;
  const refreshLimit = $("#smart-scan-due-limit");
  if (state.smartScanDueHydrationEnabled && refreshLimit && !refreshLimit.reportValidity()) return;

  const searchTerms = $("#smart-scan-discovery-terms");
  const terms = searchTerms && !searchTerms.disabled
    ? (searchTerms.value || "").split(/\r?\n/).map(term => term.trim().replace(/\s+/g, " ")).filter(Boolean)
    : [];
  if (terms.length > 5 || terms.some(term => Array.from(term).length > 100)) {
    searchTerms.setCustomValidity("Enter up to five search terms of 100 characters each, one per line.");
    searchTerms.reportValidity();
    return;
  }
  const optionsDialog = $("#smart-scan-options-dialog");
  if (optionsDialog?.open) optionsDialog.close();
  const requestedSiteLimit = Number($("#smart-scan-ai-sites")?.value || state.smartScanAISiteLimit);
  const siteLimit = Number.isInteger(requestedSiteLimit) && requestedSiteLimit >= 1
    ? requestedSiteLimit
    : Number(state.smartScan?.profile?.candidate_ai_call_cap || 25);
  const requestedSEOSiteLimit = Number($("#smart-scan-seo-sites")?.value || state.smartScanSEOSiteLimit);
  const seoSiteLimit = Number.isInteger(requestedSEOSiteLimit) && requestedSEOSiteLimit >= 1
    ? requestedSEOSiteLimit
    : Number(state.smartScan?.profile?.dataforseo_target_default || 1000);
  const previous = state.smartScan || {};
  state.smartScanStarting = true;
  state.smartScanTerminalTab = "activity";
  ++smartScanLoadGeneration;
  clearTimeout(state.smartScanPollTimer);
  renderSmartScan({
    ...previous, id: null, status: "starting", phase: "planning", preparing: true,
    finished_at: null, resumed_after_restart_at: null,
    due_hydration_enabled: state.smartScanDueHydrationEnabled,
    due_company_limit: state.smartScanDueCompanyLimit ?? null,
    paid_ads_enabled: Boolean(state.smartScanAdsEnabled),
    paid_ads_company_limit: state.smartScanAdsCompanyLimit,
    paid_ads_max_usd: state.smartScanAdsMaxUSD,
    people_hydration_enabled: Boolean(state.smartScanPeopleEnabled),
    people_company_limit: state.smartScanPeopleCompanyLimit,
    ai_enabled: state.smartScanAIEnabled, ai_site_limit: siteLimit,
    source_ai_enabled: selectedSourceAI(),
    microlink_enabled: selectedMicrolink(),
    microlink_hydration: previous.profile?.microlink_hydration_available && previous.profile?.microlink_configured
      ? microlinkHydrationOptions() : {},
    dataforseo_enabled: state.smartScanSEOEnabled, seo_site_limit: seoSiteLimit,
    max_company_failure_rate: state.smartScanFailureSkipEnabled ? Number(state.smartScanFailureRate) : null,
    started_at: new Date().toISOString(), message: "Requesting scan preparation",
    progress_percent: 0, phase_index: 1, control_status: "preparing",
    actions: [], counters: {}, error_total: 0,
    work: {completed: 0, total: 0, current: "Requesting scan preparation"},
    events: [{timestamp: new Date().toISOString(), phase: "planning", level: "info",
      message: "Requesting scan preparation", detail: ""}],
  });
  state.smartScanPollTimer = setTimeout(pollSmartScan, 100);
  try {
    const scan = await api("/api/smart-scan", {
      method: "POST",
      body: JSON.stringify({
        mission_id:selectedScanMission(),
        search_terms: [...new Set(terms)],
        due_hydration_enabled: state.smartScanDueHydrationEnabled,
        ...(state.smartScanDueHydrationEnabled && state.smartScanDueCompanyLimit != null
          ? {due_company_limit:state.smartScanDueCompanyLimit} : {}),
        ...(previous.profile?.paid_ads_included ? {
          paid_ads_enabled: Boolean(state.smartScanAdsEnabled),
          paid_ads_company_limit: state.smartScanAdsEnabled ? state.smartScanAdsCompanyLimit : 100,
          paid_ads_max_usd: state.smartScanAdsEnabled ? state.smartScanAdsMaxUSD : 5,
        } : {}),
        people_hydration_enabled: Boolean(state.smartScanPeopleEnabled),
        people_company_limit: state.smartScanPeopleCompanyLimit,
        ai_enabled: state.smartScanAIEnabled,
        ...(previous.profile?.source_ai_control_available ? {source_ai_enabled:selectedSourceAI()} : {}),
        ...(previous.profile?.microlink_control_available ? {microlink_enabled:selectedMicrolink()} : {}),
        ...(previous.profile?.microlink_hydration_available && previous.profile?.microlink_configured
          ? {microlink_hydration:microlinkHydrationOptions()} : {}),
        ai_site_limit: siteLimit,
        seo_enabled: state.smartScanSEOEnabled,
        seo_site_limit: seoSiteLimit,
        max_company_failure_rate: state.smartScanFailureSkipEnabled
          ? Number(state.smartScanFailureRate)
          : null,
      }),
    });
    renderSmartScan(scan);
    showToast("Smart Scan started");
  } catch (error) {
    renderSmartScan(previous);
    showToast(error.message, "error");
  } finally {
    state.smartScanStarting = false;
    ++smartScanLoadGeneration;
    clearTimeout(state.smartScanPollTimer);
    renderSmartScan(state.smartScan || {});
    if (smartScanBusy()) {
      state.smartScanPollTimer = setTimeout(pollSmartScan, 100);
    } else if (state.smartScan?.inventory_pending && !state.smartScan.inventory_error) {
      void refreshSmartScanInventory(state.smartScan, smartScanLoadGeneration);
    }
  }
}

async function pollSmartScan() {
  if (state.smartScanPollInFlight || state.activeView !== "smart_scan") return;
  if (state.smartScanControlPending) {
    state.smartScanPollTimer = setTimeout(pollSmartScan, 500);
    return;
  }
  clearTimeout(state.smartScanPollTimer);
  state.smartScanPollInFlight = true;
  const wasActive = smartScanBusy();
  const generation = smartScanLoadGeneration;
  const previous = state.smartScan;
  try {
    const scan = await api("/api/smart-scan?fast=true");
    if (generation !== smartScanLoadGeneration || state.activeView !== "smart_scan") return;
    if (state.smartScan !== previous) {
      if (state.smartScanStarting || smartScanBusy()) state.smartScanPollTimer = setTimeout(pollSmartScan, 500);
      return;
    }
    // The first poll can beat the POST to the server. Keep preparation visible
    // until its authoritative status arrives; never refresh full inventory here.
    if (state.smartScanStarting && !smartScanActive(scan)) {
      state.smartScanPollTimer = setTimeout(pollSmartScan, 500);
      return;
    }
    renderSmartScan(scan);
    if (state.smartScanStarting || smartScanBusy(scan)) {
      state.smartScanPollTimer = setTimeout(pollSmartScan, 500);
    } else if (wasActive) {
      if (scan.status === "stopped") void pollScanStatus();
      showToast(scan.message || "Smart Scan finished", scanToastTone(scan.status));
      await refreshVisibleData();
      if (scan.inventory_pending) void refreshSmartScanInventory(scan, generation);
    }
  } catch {
    state.smartScanPollTimer = setTimeout(pollSmartScan, 4000);
  } finally {
    state.smartScanPollInFlight = false;
  }
}

function scanMethodsShell() {
  state.webScanEstimate = null;
  state.adDiscoveryEstimate = null;
  $("#scan-methods-content").innerHTML = `
    ${discoveryTabListMarkup()}
    <section ${discoveryPanelAttributes("web_ct")}>
    <div class="web-scan-layout">
      <form class="panel web-scan-builder" id="web-scan-form">
        <div class="panel-head"><div class="scan-card-title"><h3>Web &amp; CT scan</h3>${scanCardHelp("web-ct", "Web & CT", SCAN_METHOD_HELP.web_ct)}</div><span class="badge moss">Public retrieval $0.00</span></div>
        <div class="web-scan-body">
          <div class="web-scan-fields">
            <label class="field"><span>Max hostnames per source</span><input name="max_hosts_per_source" type="number" min="1" max="10000" value="10000" required /></label>
            <label class="field"><span>Max website inspections</span><input name="max_network_inspections" type="number" min="1" max="10000" value="1000" required /></label>
          </div>
          <div class="web-scan-fixed"><label class="paid-check" title="Required scan policy"><input type="checkbox" checked disabled /><span>English only</span></label></div>
          <div class="paid-check-grid">
            <label class="paid-check"><input name="free_intelligence" type="checkbox" checked /><span>Site intelligence</span></label>
            <label class="paid-check"><input name="agency_discovery" type="checkbox" checked /><span>Agency scan</span></label>
            <label class="paid-check"><input name="people_hydration_enabled" type="checkbox" /><span>People hydration · paid</span></label>
            <label class="paid-check"><input name="microlink_enabled" type="checkbox" /><span>Microlink fallback · paid usage</span></label>
          </div>
        </div>
      </form>
      <aside class="panel web-scan-quote" id="web-scan-quote" aria-live="polite" hidden></aside>
    </div>
    </section>
    ${discoveryTabs().filter(tab => tab.kind === "method").map(discoveryMethodPanelShell).join("")}
    <section ${discoveryPanelAttributes("paid_ads")}>
    <div class="paid-discovery-layout">
      <form class="panel paid-discovery-builder" id="paid-discovery-form">
        <div class="panel-head"><div class="scan-card-title"><h3>Paid advertising</h3>${scanCardHelp("paid-ads", "Paid advertising", SCAN_METHOD_HELP.paid_ads)}</div><div class="paid-discovery-states"><span class="badge" id="paid-discovery-backlog-state">Checking backlog…</span><span class="badge" id="paid-discovery-key-state">Checking Apify…</span></div></div>
        <div class="paid-form-body">
          <p class="paid-discovery-backlog-copy" id="paid-discovery-backlog-copy">Loading the existing-target Meta scan inventory…</p>
          <div class="paid-field-grid">
            <label class="field"><span>Mode</span><select name="mode"><option value="discover" selected>Discover new companies</option><option value="hydrate">Hydrate existing targets</option><option value="both">Discover + hydrate existing</option></select></label>
            <label class="field"><span>Country code or ALL</span><input name="country" value="US" maxlength="3" pattern="([A-Za-z]{2}|[Aa][Ll][Ll])" required autocomplete="off" /></label>
            <label class="field paid-query-field"><span>Meta keyword missions · one per line</span><textarea name="meta_queries" rows="4" placeholder="subscription skincare&#10;mobile game" spellcheck="false">${escapeHtml(DEFAULT_META_DISCOVERY_QUERIES.join("\n"))}</textarea></label>
          </div>
          <fieldset class="paid-fieldset">
            <legend>Meta scan and follow-up</legend>
            <div class="paid-limit-grid">
              <label class="field"><span>Ads / query</span><input name="meta_ads_per_query" type="number" min="1" max="500" value="500" required /></label>
              <label class="field"><span>New advertisers</span><input name="max_new_advertisers" type="number" min="0" max="500" value="500" required /></label>
              <label class="field"><span>Deep ads / advertiser</span><input name="meta_ads_per_advertiser" type="number" min="1" max="500" value="230" required /></label>
              <label class="field"><span>Existing targets</span><input name="existing_target_limit" type="number" min="0" max="500" value="0" required /></label>
            </div>
            <div class="paid-check-grid">
              <label class="paid-check"><input name="meta_enabled" type="checkbox" checked /><span><strong>Meta paid ads</strong></span></label>
              <label class="paid-check"><input name="active_only" type="checkbox" checked /><span><strong>Active ads only</strong></span></label>
            </div>
          </fieldset>
          <fieldset class="paid-fieldset">
            <legend>Optional TikTok coverage</legend>
            <div class="paid-check-grid">
              <label class="paid-check"><input name="tiktok_library_enabled" type="checkbox" /><span><strong>Commercial Content Library</strong></span></label>
              <label class="field inline-limit"><span>Ads / advertiser</span><input name="tiktok_ads_per_advertiser" type="number" min="1" max="120" value="120" required /></label>
              <label class="paid-check"><input name="tiktok_top_enabled" type="checkbox" /><span><strong>TikTok Top Ads</strong></span></label>
              <label class="field inline-limit"><span>Top ads / advertiser</span><input name="tiktok_top_ads_per_advertiser" type="number" min="1" max="100" value="100" required /></label>
            </div>
          </fieldset>
          <fieldset class="paid-fieldset">
            <legend>Identity and website hydration</legend>
            <div class="paid-check-grid">
              <label class="paid-check"><input name="verify_landing_domains" type="checkbox" checked /><span><strong>Verify landing domains</strong></span></label>
              <label class="paid-check"><input name="hydrate_websites" type="checkbox" checked /><span><strong>Inspect verified websites</strong></span></label>
              <label class="paid-check"><input type="checkbox" checked disabled /><span><strong>English-presenting sites only</strong></span></label>
              <label class="field inline-limit"><span>Website hydration limit</span><input name="website_hydration_limit" type="number" min="0" max="100" value="100" required /></label>
            </div>
          </fieldset>
          <label class="field paid-budget-field"><span>Maximum provider spend · USD</span><div><span>$</span><input name="max_total_charge_usd" type="number" min="0.10" max="100" step="0.10" value="100.00" required /></div></label>
        </div>
      </form>
      <aside class="panel paid-quote-panel" id="paid-discovery-quote" aria-live="polite" hidden></aside>
    </div>
    <section class="paid-run-region" id="paid-discovery-run" aria-live="polite"></section>
    <div class="paid-results-grid">
      <section class="panel">
        <div class="panel-head"><div><h3>Advertiser ↔ domain review</h3><span class="section-meta" id="paid-link-count">Loading…</span></div><label class="compact-field"><span>State</span><select id="paid-link-filter"><option value="all">All</option><option value="verified">Verified</option><option value="probable">Probable</option><option value="ambiguous">Ambiguous</option><option value="rejected">Rejected</option></select></label></div>
        <div class="table-wrap"><table class="paid-link-table"><thead><tr><th>Advertiser</th><th>Domain</th><th>Identity</th><th>Evidence</th><th>Candidate</th><th></th></tr></thead><tbody id="paid-link-body"><tr><td colspan="6" class="loading-row">${loadingIndicator()}</td></tr></tbody></table></div>
      </section>
    </div>
    </section>
    <section ${discoveryPanelAttributes("find_sources")}>
      <section class="discovery-scout-controls panel">
        <header class="panel-head"><div><h3>AI source scout</h3><p>OpenAI usage · capped by your web-call limit</p></div><span class="badge" id="scout-state" data-scout-state>Idle</span></header>
        <form id="source-scout-form" data-source-scout-form>
          <label><span>Max suggestions</span><input name="max_suggestions" type="number" min="1" max="12" value="12" required /></label>
          <label><span>Max OpenAI web calls</span><input name="max_tool_calls" type="number" min="1" max="10" value="10" required /></label>
          <button class="button primary" id="run-source-scout" data-source-scout-start="custom" type="submit" disabled>Checking readiness…</button>
        </form>
      </section>
      <div class="scout-message" id="scout-message" data-scout-message aria-live="polite" hidden></div>
      <section class="panel scout-results-panel">
      <header class="panel-head"><h3>Source ideas</h3><div class="intelligence-stats" id="intelligence-stats" data-scout-stats></div></header>
      <div class="table-wrap"><table class="source-table">
        <thead><tr><th>Source</th><th>Kind</th><th>Score</th><th>Mechanism</th><th>Evidence</th><th>Status</th><th></th></tr></thead>
        <tbody id="suggestion-body" data-suggestion-body><tr><td colspan="7" class="loading-row">${loadingIndicator()}</td></tr></tbody>
      </table></div></section>
    </section>`;
  addScanFieldHelp($("#scan-methods-content"));
  const webForm = $("#web-scan-form");
  syncDiscoveryProviderChoices();
  bindAutomaticScanEstimate(webForm, estimateWebScan, invalidateWebScanEstimate);
  const paidForm = $("#paid-discovery-form");
  bindAutomaticScanEstimate(paidForm, estimateAdDiscovery, handleAdDiscoveryDraftChange);
  $("#paid-link-filter").addEventListener("change", event => {
    state.adDiscoveryLinkFilter = event.target.value;
    renderAdDiscoveryLinks();
  });
  $("#source-scout-form").addEventListener("submit", startSourceScout);
  syncAdDiscoveryForm();
  enhanceMultiChecklists($("#scan-methods-content"));
  const dialog = $("#smart-scan-options-dialog");
  let navigation = $(".scan-pinned-navigation", dialog);
  if (!navigation) {
    navigation = document.createElement("div");
    navigation.className = "scan-pinned-navigation";
    $(".modal-head", dialog).append(navigation);
  }
  const presets = document.createElement("div");
  presets.className = "scan-preset-picker";
  presets.setAttribute("role", "group");
  presets.setAttribute("aria-label", "Scan presets");
  presets.append($("#scan-basic-option", dialog), $("#saved-scan-goals", dialog));
  const tabs = $(".discovery-tabs", dialog);
  const firstMethod = $("[data-discovery-tab]", tabs);
  for (const [id, label] of SCAN_SECTION_LINKS) {
    const section = $(`#${id}`, dialog).closest(".scan-research-panel");
    section.id = `${id}-section`;
    const link = document.createElement("button");
    link.type = "button";
    link.className = "discovery-tab scan-section-link";
    link.textContent = label;
    link.setAttribute("aria-controls", section.id);
    link.dataset.discoveryTab = id;
    tabs.insertBefore(link, firstMethod);
  }
  bindDiscoveryTabs();
  selectDiscoveryTab(state.discoveryTab);
  navigation.append($("#scan-goal-form", dialog), presets, $(".discovery-tab-shell", dialog));
  if (!$("#find-directories-dialog")) {
    const scoutDialog = document.createElement("dialog");
    scoutDialog.id = "find-directories-dialog";
    scoutDialog.className = "modal smart-scan-options-dialog standalone-scan-dialog";
    scoutDialog.dataset.scanMode = "sources";
    scoutDialog.setAttribute("aria-labelledby", "find-directories-title");
    scoutDialog.innerHTML = `<header class="modal-head"><h2 id="find-directories-title" tabindex="-1" autofocus>Find directories</h2><button type="button" class="icon-button" data-close-dialog="find-directories-dialog" aria-label="Close find directories">×</button></header><div class="modal-body scan-setup-body"></div><footer class="modal-actions"><button class="button" type="button" data-close-dialog="find-directories-dialog">Close</button></footer>`;
    const panel = $("#discovery-panel-find_sources", dialog);
    panel.removeAttribute("data-discovery-panel");
    panel.removeAttribute("aria-labelledby");
    panel.className = "source-scout-content";
    panel.hidden = false;
    $(".scan-setup-body", scoutDialog).append(panel);
    dialog.parentElement.append(scoutDialog);
    bindScanCardHelp(scoutDialog);
  }

}

function syncScanGoalHelper(dialog, mode) {
  const helper = $("#scan-goal-form", dialog);
  if (helper) helper.hidden = mode !== "smart";
}

function setScanImportMode(importing) {
  const dialog = $("#import-companies-dialog");
  if (importing) {
    if (!dialog.open) dialog.showModal();
    $("#import-scan-form").elements.urls.focus({preventScroll:true});
  } else {
    if (dialog.open) dialog.close();
    $("#import-companies-trigger").focus({preventScroll:true});
  }
}

function bindMicrolinkScanBar(root) {
  const stop = $("#stop-smart-scan", root);
  const pauseResume = $("#pause-resume-smart-scan", root);
  const lanes = new Map();
  let last = null;
  let shown = null;
  const active = result => ["running", "stopping"].includes(result.status);

  function render() {
    const running = [...lanes.values()].find(lane => active(lane.result));
    if (state.microlinkScanActive !== Boolean(running)) {
      state.microlinkScanActive = Boolean(running);
      updateSmartScanLaunchControls();
    }
    const scan = state.smartScan || {};
    shown = running || (smartScanBusy(scan) || scan.external_source_runs?.length ? null : last);
    state.microlinkRunControl = shown?.control ?? null;
    state.microlinkVisibleErrors = shown ? shown.result.errors || [] : null;
    if (!shown) {
      state.microlinkOverview = null;
      return;
    }
    const {kind, result, pending, reconnecting} = shown;
    const discovery = kind === "discovery";
    const name = discovery ? "Microlink company search" : "Microlink contacts";
    const phase = result.status;
    const progress = discovery
      ? `${formatNumber(result.requests ?? 0)}/${formatNumber(result.max_requests ?? 0)} requests reserved · ${formatNumber(result.candidates ?? 0)} new companies`
      : `${formatNumber(result.completed ?? 0)}/${formatNumber(result.companies ?? 0)} companies processed · ${formatNumber(result.failed ?? 0)} failed · ${formatNumber(result.requests ?? 0)}/${formatNumber(result.max_requests ?? 0)} requests`;
    $("#smart-scan-run-controls", root).hidden = !active(result) && !result.resumable;
    $("#smart-scan-phase-label", root).textContent = [name, result.mission, reconnecting ? "Reconnecting…" : ""].filter(Boolean).join(" · ");
    const done = discovery ? result.completed_requests : (result.completed || 0) + (result.failed || 0);
    const cap = discovery ? result.max_requests : result.companies;
    const percent = cap ? Math.min(100, Math.round((done || 0) / cap * 100)) : 0;
    state.microlinkOverview = {label:name, status:phase, run:{
      ...result, progress_percent:percent, new_companies_added:discovery ? result.candidates : undefined,
    }};
    $("#smart-scan-percent", root).textContent = `${percent}%`;
    $("#smart-scan-track-fill", root).style.width = `${percent}%`;
    const track = $("#smart-scan-track", root);
    track.setAttribute("aria-valuenow", percent);
    track.setAttribute("aria-valuetext", progress);
    track.title = discovery ? "Share of request cap processed" : "Share of companies processed";
    const phaseList = $("#smart-scan-phases", root);
    phaseList.hidden = true;
    phaseList.replaceChildren();
    delete phaseList.dataset.renderedMarkup;
    $("#smart-scan-note", root).textContent = progress;
    const errors = result.errors || [];
    const failures = result.error_total ?? result.failed ?? 0;
    const errorTab = state.smartScanTerminalTab === "errors";
    const events = errorTab ? errors : [
      ...(result.events || []),
      {message: progress, level: phase === "complete" ? "success" : "info"},
    ];
    $("#smart-terminal-error-count", root).textContent = formatNumber(failures);
    const copy = $("#copy-smart-scan-errors", root);
    copy.hidden = !errorTab;
    $("#smart-terminal-error-note", root).hidden = !errorTab;
    copy.disabled = !errors.length;
    const terminal = $("#smart-terminal-lines", root);
    const follow = smartScanTerminalNearBottom(terminal);
    const rows = events.map(smartScanTerminalLine);
    if (!rows.length) rows.push('<div class="smart-terminal-empty">No failures recorded.</div>');
    renderTerminalRows(terminal, rows, {follow:follow && !errorTab});
    highlightLatestTerminalActivity(terminal, !errorTab && active(result));
    $("#smart-terminal-meta", root).textContent = errorTab
      ? `${formatNumber(errors.length)} / ${formatNumber(failures)} failures`
      : "";
    $("#smart-terminal-elapsed", root).textContent = active(result) || result.finished_at
      ? smartScanElapsedLabel(result) : "";
    root.dataset.status = phase;
    stop.hidden = !active(result) || phase === "stopping";
    stop.disabled = pending || result.status === "stopping";
    pauseResume.value = "resume";
    pauseResume.textContent = "Resume";
    pauseResume.hidden = active(result) || !result.resumable;
    pauseResume.disabled = pending;
    renderAggregateScanState();
    return true;
  }

  function monitor(kind) {
    const endpoint = `/api/microlink-${kind}`;
    const lane = {kind, result: {status: "idle"}, pending: false, reconnecting: false, statusChecked: false};
    lanes.set(kind, lane);
    let timer;
    let revision = 0;
    let reading = false;
    const schedule = delay => {
      clearTimeout(timer);
      timer = setTimeout(refresh, delay);
    };
    const accept = result => {
      revision += 1;
      const wasActive = active(lane.result);
      lane.result = result;
      lane.reconnecting = false;
      if (result.status === "idle" && last === lane) last = null;
      else if (active(result) || (wasActive && result.status !== "idle") || (result.resumable && !last)) last = lane;
      if (wasActive && !active(result)) invalidateSalesWorkbench();
      render();
      lane.onUpdate?.();
      clearTimeout(timer);
      if (active(result)) schedule(1500);
    };
    async function refresh() {
      if (reading || lane.pending) return;
      clearTimeout(timer);
      reading = true;
      const token = revision;
      try {
        const result = await api(`${endpoint}/status`, {signal: AbortSignal.timeout(8000)});
        if (token === revision && !lane.pending) accept(result);
      } catch {
        if (token !== revision || lane.pending) return;
        if (active(lane.result)) {
          lane.reconnecting = true;
          render();
        }
        if (active(lane.result) || state.activeView === "smart_scan") schedule(3000);
      } finally {
        reading = false;
        lane.statusChecked = true;
        if ([...lanes.values()].every(item => item.statusChecked)) delete root.dataset.microlinkStatusPending;
        renderScanLauncherStatus();
      }
    }
    root.addEventListener("microlink-refresh", refresh);
    lane.control = async action => {
      if (lane.pending) return;
      lane.pending = true;
      revision += 1;
      render();
      try {
        const result = await api(`${endpoint}/${action}`, {method: "POST"});
        accept(action === "stop" && result.status === "stopping" ? {...lane.result, ...result} : result);
      } catch (error) {
        showToast(error.message, "error");
      } finally {
        lane.pending = false;
        render();
        schedule(1500);
      }
    };
    return {
      endpoint, refresh, active: () => active(lane.result),
      onUpdate: callback => { lane.onUpdate = callback; },
      started(result, dialog) {
        accept(result);
        if (dialog.open) {
          dialog.close();
          stop.focus();
        }
      },
    };
  }

  root.addEventListener("microlink-render", event => {
    if (render()) event.preventDefault();
  });
  return monitor;
}

function bindScanCardHelp(dialog) {
  addScanFieldHelp(dialog);
  const body = $(".scan-setup-body", dialog);
  let trigger;
  let tooltip;
  let pinned = false;
  let hideTimer;

  function hide() {
    clearTimeout(hideTimer);
    if (tooltip?.matches(":popover-open")) tooltip.hidePopover();
    trigger = tooltip = null;
    pinned = false;
    window.removeEventListener("resize", position);
  }

  function position() {
    if (!tooltip) return;
    const anchor = trigger.getBoundingClientRect();
    const viewport = body.getBoundingClientRect();
    if (anchor.bottom <= viewport.top || anchor.top >= viewport.bottom) {
      hide();
      return;
    }
    const gap = 6;
    const margin = 12;
    const {clientHeight: height, clientWidth: width} = document.documentElement;
    const below = height - anchor.bottom - gap - margin;
    const above = anchor.top - gap - margin;
    const placeBelow = below >= tooltip.scrollHeight || below >= above;
    tooltip.style.maxHeight = `${placeBelow ? below : above}px`;
    const left = Math.max(margin, Math.min(anchor.left, width - tooltip.offsetWidth - margin));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${placeBelow ? anchor.bottom + gap : anchor.top - gap - tooltip.offsetHeight}px`;
  }

  function show(button) {
    clearTimeout(hideTimer);
    if (trigger === button) return;
    hide();
    trigger = button;
    tooltip = button.nextElementSibling;
    tooltip.showPopover();
    position();
    if (tooltip) window.addEventListener("resize", position);
  }

  function deferHide() {
    clearTimeout(hideTimer);
    if (!pinned) hideTimer = setTimeout(hide, 180);
  }

  dialog.addEventListener("pointerover", event => {
    const button = event.target.closest(".scan-card-info");
    if (tooltip?.contains(event.target)) clearTimeout(hideTimer);
    if (button && event.pointerType === "mouse" && !pinned) show(button);
  });
  dialog.addEventListener("pointerout", event => {
    const button = event.target.closest(".scan-card-info");
    if ((button && button === trigger || tooltip?.contains(event.target))
      && !trigger?.matches(":focus-visible")) deferHide();
  });
  dialog.addEventListener("focusin", event => {
    if (event.target.matches(".scan-card-info:focus-visible")) show(event.target);
  });
  dialog.addEventListener("focusout", event => {
    if (event.target === trigger) hide();
  });
  dialog.addEventListener("click", event => {
    const button = event.target.closest(".scan-card-info");
    if (!button) return;
    if (trigger === button && pinned) hide();
    else {
      show(button);
      pinned = Boolean(tooltip);
    }
  });

  body.addEventListener("scroll", position);
  dialog.addEventListener("pointerdown", event => {
    if (tooltip && !tooltip.contains(event.target) && !trigger.contains(event.target)) hide();
  });
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !tooltip) return;
    event.preventDefault();
    hide();
  });
  dialog.addEventListener("close", hide);
}

const SAVED_SCAN_GOALS_KEY = "beings.scan.saved-goals.v1";
function readSavedScanGoals() {
  try {
    const saved = JSON.parse(localStorage.getItem(SAVED_SCAN_GOALS_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter(item => typeof item?.goal === "string"
      && item.goal.trim() && item.settings && typeof item.settings === "object") : [];
  } catch { return []; }
}

function selectedScanMission() {
  try { return localStorage.getItem("beings-mission") || "beings"; }
  catch { return "beings"; }
}
async function seedMissionScanTerms(dialog) {
  const mission = selectedScanMission();
  const context = await api(`/api/scans/mission-context?mission_id=${encodeURIComponent(mission)}`);
  if (!dialog.open || selectedScanMission() !== mission) return;
  state.scanMissionContext = context;
  const terms = (context.search_terms || []).filter(term => typeof term === "string");
  const fields = [
    [$("#smart-scan-discovery-terms", dialog), terms.slice(0, 5).join("\n")],
    [$("[name=meta_queries]", dialog), terms.slice(0, 20).join("\n")],
  ];
  for (const [field, value] of fields) {
    if (!field) continue;
    const previous = field.value;
    const untouched = !field.value.trim() || field.value === field.dataset.missionSeed
      || (field.name === "meta_queries" && field.value === DEFAULT_META_DISCOVERY_QUERIES.join("\n"));
    if (!untouched) continue;
    if (value || field.dataset.missionSeed) field.value = value;
    field.dataset.missionSeed = value;
    if (field.name === "meta_queries" && field.value !== previous && state.discoveryTab === "paid_ads") {
      scanEstimateRefresh.get(field.form)?.();
    }
    if (field.id === "smart-scan-discovery-terms") state.smartScanSearchTermsDraft = field.value;
  }
}

function bindScanGoal(dialog) {
  const form = $("#scan-goal-form", dialog);
  const status = $("#scan-goal-status", form);
  const goalInput = form.elements.goal;
  const send = $('button[type="submit"]', form);
  const configuringLabel = $("[data-configuring-label]", send);
  const sendLabel = send.getAttribute("aria-label");
  let revision = 0;
  function showStatus(text, configured = false) {
    status.textContent = text;
    status.classList.toggle("scan-goal-confirmation", configured);
  }
  function applySettings(settings, presetKey) {
      const fields = {
        due_hydration_enabled:"smartScanDueHydrationEnabled",
        due_company_limit:"smartScanDueCompanyLimit",
        source_ai_enabled:"smartScanSourceAIEnabled", microlink_enabled:"smartScanMicrolinkEnabled",
        ai_enabled:"smartScanAIEnabled", ai_site_limit:"smartScanAISiteLimit",
        seo_enabled:"smartScanSEOEnabled", seo_site_limit:"smartScanSEOSiteLimit",
        people_hydration_enabled:"smartScanPeopleEnabled", people_company_limit:"smartScanPeopleCompanyLimit",
        paid_ads_enabled:"smartScanAdsEnabled", paid_ads_company_limit:"smartScanAdsCompanyLimit",
        paid_ads_max_usd:"smartScanAdsMaxUSD", microlink_hydration:"smartScanMicrolinkHydration",
      };
      for (const field of Object.keys(fields)) {
        if (!Object.hasOwn(settings || {}, field)) throw new Error("AI configuration was incomplete.");
      }
      const terms = settings.search_terms;
      if (!Array.isArray(terms) || terms.length > 5
        || terms.some(term => typeof term !== "string" || !term.trim() || term.length > 100 || /[\r\n]/.test(term))) {
        throw new Error("AI configuration did not include valid search terms. Please try again after the server update.");
      }
      state.smartScanSearchTermsDraft = [...new Set(terms.map(term => term.trim()))].join("\n");
      $("#smart-scan-discovery-terms", dialog).value = state.smartScanSearchTermsDraft;
      for (const [field, key] of Object.entries(fields)) state[key] = settings[field];
      state.smartScanPreset = presetKey;
      renderPresets();
      // Show the configured scan draft.
      dialog.dispatchEvent(new Event("scan-goal-applied"));
      for (const panel of dialog.querySelectorAll("[data-scan-setup-panel]")) {
        panel.hidden = false;
      }
      $("#smart-scan-options-title", dialog).textContent = "Smart Scan";
      $("#smart-scan-options-subtitle", dialog).textContent = "";
      $("#scan-setup-start", dialog).hidden = true;
      $("#start-smart-scan", dialog).hidden = false;
      updateSmartScanLaunchControls();
      $(".scan-option-panel[data-scan-setup-panel='smart']", dialog)?.scrollIntoView({behavior:"smooth", block:"start"});
  }
  function renderPresets() {
    $("#scan-basic-option", dialog).setAttribute("aria-pressed", String(state.smartScanPreset === "basic"));
    const list = $("#saved-scan-goals", dialog);
    list.innerHTML = readSavedScanGoals().map((preset, index) => `<div class="saved-scan-goal-row"><button type="button" class="button saved-scan-goal" aria-pressed="${state.smartScanPreset === `${preset.mission_id || selectedScanMission()}:${preset.goal}`}" data-saved-scan-goal="${index}" title="${escapeHtml(preset.goal)}"><span class="scan-basic-dot" aria-hidden="true"></span><span>${escapeHtml(preset.goal)}</span></button><button type="button" class="saved-scan-goal-delete" data-delete-scan-goal="${index}" aria-label="Delete saved scan: ${escapeHtml(preset.goal)}" title="Delete saved scan">×</button></div>`).join("");
  }
  renderPresets();
  $("#scan-basic-option", dialog).addEventListener("click", () => {
    if (state.smartScanGoalPending || scanLaunchConflict("smart")) return;
    state.smartScanFailureSkipEnabled = false;
    state.smartScanFailureRate = 75;
    applySettings({
      due_hydration_enabled:true, due_company_limit:null, search_terms:[],
      source_ai_enabled:false, microlink_enabled:false,
      ai_enabled:false, ai_site_limit:100, seo_enabled:false, seo_site_limit:100,
      people_hydration_enabled:false, people_company_limit:100,
      paid_ads_enabled:false, paid_ads_company_limit:100, paid_ads_max_usd:5,
      microlink_hydration:{company_limit:100, request_limit:500},
    }, "basic");
    form.elements.goal.value = "";
    showStatus("");
    revision++;
    void seedMissionScanTerms(dialog).catch(error => { showStatus(error.message); });
  });
  $("#saved-scan-goals", dialog).addEventListener("click", event => {
    const remove = event.target.closest("[data-delete-scan-goal]");
    const select = event.target.closest("[data-saved-scan-goal]");
    if (!remove && !select) return;
    if (state.smartScanGoalPending || scanLaunchConflict("smart")) return;
    const presets = readSavedScanGoals();
    try {
      if (remove) {
        const index = Number(remove.dataset.deleteScanGoal);
        presets.splice(index, 1);
        localStorage.setItem(SAVED_SCAN_GOALS_KEY, JSON.stringify(presets));
        renderPresets();
        form.elements.goal.focus({preventScroll:true});
      } else {
        const preset = presets[Number(select.dataset.savedScanGoal)];
        if (!preset) return;
        if (preset.mission_id && preset.mission_id !== selectedScanMission()) {
          throw new Error("Switch to the mission this scan was saved for to reuse its settings.");
        }
        applySettings(structuredClone(preset.settings), `${preset.mission_id || selectedScanMission()}:${preset.goal}`);
        form.elements.goal.value = preset.goal;
        revision++;
        showStatus("Saved settings applied.");
      }
    } catch (error) { showStatus(error.message); }
  });
  dialog.addEventListener("input", event => {
    if (event.target !== goalInput && !event.target.closest(".scan-option-panel[data-scan-setup-panel='smart']")) return;
    revision++;
    if (!state.smartScanGoalPending) showStatus("");
  });
  dialog.addEventListener("close", () => { revision++; });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (state.smartScanGoalPending || smartScanLaunchBlocked() || !form.reportValidity()) return;
    const goal = form.elements.goal.value.trim();
    const requestedRevision = revision;
    const mission = selectedScanMission();
    state.smartScanGoalPending = true;
    goalInput.readOnly = true;
    configuringLabel.hidden = false;
    send.setAttribute("aria-label", "Configuring…");
    showStatus("");
    updateSmartScanLaunchControls();
    try {
      const result = await api("/api/smart-scan/configure", {
        method:"POST", body:JSON.stringify({goal, mission_id:mission, confirmed_maximum_microusd:20000}),
      });
      if (!dialog.open || selectedScanMission() !== mission) return;
      if (revision !== requestedRevision || goalInput.value.trim() !== goal) {
        showStatus("The scan settings changed during configuration.");
        return;
      }
      const conflict = scanLaunchConflict("smart");
      if (conflict) {
        showStatus(conflict);
        return;
      }
      applySettings(result.settings, `${mission}:${goal}`);
      const presets = readSavedScanGoals();
      const preset = {goal, mission_id:mission, settings:result.settings};
      const existing = presets.findIndex(item => item.goal === goal
        && item.mission_id === mission);
      if (existing >= 0) presets[existing] = preset;
      else presets.push(preset);
      localStorage.setItem(SAVED_SCAN_GOALS_KEY, JSON.stringify(presets));
      renderPresets();
      showStatus("The scan settings below have been configured for your goal.", true);
    } catch (error) {
      showStatus(error.message);
    } finally {
      state.smartScanGoalPending = false;
      goalInput.readOnly = false;
      configuringLabel.hidden = true;
      send.setAttribute("aria-label", sendLabel);
      updateSmartScanLaunchControls();
    }
  });
}

function bindScanSetup(root, monitor) {
  const dialog = $("#smart-scan-options-dialog", root);
  bindScanCardHelp(dialog);
  bindScanCardHelp($("#import-companies-dialog"));
  bindScanGoal(dialog);
  const methods = $("#scan-methods-disclosure", dialog);
  const basicPanel = $(".scan-option-panel[data-scan-setup-panel='smart']", dialog);
  methods.before(basicPanel);
  for (const group of basicPanel.querySelectorAll(":scope > .scan-option-group")) {
    const rows = $(":scope > .scan-option-rows", group);
    if (!rows || !$(".scan-card-title", rows)) continue;
    $(":scope > h3", group)?.remove();
    group.removeAttribute("aria-labelledby");
    rows.className = "scan-research-panels";
    for (const row of rows.children) {
      const title = $(".scan-card-title", row);
      if (!title) continue;
      row.classList.add("panel", "scan-research-panel");
      const head = document.createElement("div");
      head.className = "panel-head";
      const heading = document.createElement("h3");
      const label = $("label", title);
      const names = {
        "smart-scan-due-hydration":"Find & refresh companies",
        "smart-scan-skip-failures":"Repeat-failure filtering",
        "smart-scan-use-people":"Contact lookup",
      };
      if (label) {
        const name = names[$("input", label)?.id];
        if (name) $("span", label).textContent = name;
        heading.append(label);
      } else {
        const caption = $(":scope > span", title);
        if (caption) heading.append(caption);
        else {
          heading.textContent = title.textContent.trim();
          title.textContent = "";
        }
      }
      head.append(heading, ...title.childNodes);
      title.remove();
      const body = document.createElement("div");
      body.className = "scan-research-body";
      body.append(...row.childNodes);
      row.append(head, body);
    }
  }
  function showScanModes() {
    if (dialog.dataset.scanMode === "smart") {
      $("#smart-scan-options-title", dialog).textContent = "Smart Scan";
      $("#smart-scan-options-subtitle", dialog).textContent = "";
    }
    if ($("#web-scan-form")) return;
    scanMethodsShell();
    void Promise.all([loadDiscoveryMethods(), loadIntelligence(), loadAdDiscoveryOverview()]);
  }
  const discoveryForm = $("#microlink-discovery-form", dialog);
  const contactsForm = $("#microlink-contacts-form", dialog);
  const laneDialogs = {};
  for (const [kind, title, form] of [
    ["discovery", "Find companies", discoveryForm],
    ["contacts", "Find contacts", contactsForm],
  ]) {
    const modal = document.createElement("dialog");
    modal.id = kind === "discovery" ? "find-companies-dialog" : "find-contacts-dialog";
    modal.className = "modal smart-scan-options-dialog standalone-scan-dialog";
    modal.dataset.scanMode = kind;
    modal.setAttribute("aria-labelledby", `${modal.id}-title`);
    modal.innerHTML = `<header class="modal-head"><div><h2 id="${modal.id}-title" tabindex="-1" autofocus>${title}</h2></div><button type="button" class="icon-button" data-close-dialog="${modal.id}" aria-label="Close ${title.toLowerCase()}">×</button></header><div class="modal-body scan-setup-body"></div><footer class="modal-actions scan-setup-footer"><div class="scan-setup-actions"><button class="button" type="button" data-close-dialog="${modal.id}">Close</button></div></footer>`;
    $(".scan-setup-body", modal).append(form);
    form.hidden = false;
    root.append(modal);
    laneDialogs[kind] = modal;
    bindScanCardHelp(modal);
  }
  let activeDialog = dialog;
  const startButton = $("#scan-setup-start", dialog);
  const smartButton = $("#start-smart-scan", dialog);
  const status = $("#scan-setup-estimate-text", dialog);
  const laneLockNote = $("#scan-setup-lane-lock", dialog);
  const triggers = root.querySelectorAll("[data-scan-setup], #scan-methods-trigger");
  const descriptions = {
    smart: "Find new companies and refresh saved profiles.",
    discovery: "Search the web with Microlink using your selected mission or a preset. New matches are added to Companies.",
    contacts: "Use Microlink to find named people and leadership at larger companies already in your list. Companies checked in the past 90 days are skipped.",
  };
  const panels = [...dialog.querySelectorAll("[data-scan-setup-panel]")];
  const lanes = {
    discovery: {
      form: discoveryForm,
      monitor: monitor("discovery"),
      startLabel: "Start company search",
      options: fields => ({
        lane: fields.lane.value,
        request_limit: Number(fields.request_limit.value),
        candidate_limit: Number(fields.candidate_limit.value),
        prefer_large: fields.prefer_large.checked,
        limit_to_com: fields.limit_to_com.checked,
        limit_to_ai: fields.limit_to_ai.checked,
      }),
      summary: quote => quote.max_requests
        ? `Up to ${formatNumber(quote.max_requests)} Microlink search requests`
          + (quote.mission ? ` · ${quote.mission} · ${quote.markets.length} markets` : "")
        : "No fresh queries for this selection. Try another search scope or domain filter.",
    },
    contacts: {
      form: contactsForm,
      monitor: monitor("contacts"),
      startLabel: "Start contact lookup",
      options: fields => ({company_count: Number(fields.company_count.value), ...(fields.force_contacts?.checked ? {force_contacts:true} : {})}),
      summary: quote => quote.companies
        ? `${formatNumber(quote.companies)} eligible · ${formatNumber(quote.headcount_supported)} with headcount evidence · up to ${formatNumber(quote.max_requests)} Microlink requests · ${quote.mission}`
        : `No eligible large companies due · ${quote.mission}`,
    },
  };
  let mode = "smart";
  let pending = false;
  let approval = null;
  let scope = discoveryForm.elements.lane.value;

  function optionsFor(lane) {
    let mission_id = "beings";
    try { mission_id = localStorage.getItem("beings-mission") || mission_id; } catch {}
    return {...lane.options(lane.form.elements), mission_id};
  }
  function blocked() {
    const scan = state.smartScan || {};
    return scan.profile?.microlink_configured === false || pending || Boolean(scanLaunchConflict("microlink", scan));
  }
  function update() {
    const lane = lanes[mode];
    const locked = blocked();
    const lockNote = laneLockNote;
    lockNote.hidden = !lane || !locked;
    lockNote.textContent = state.smartScan?.profile?.microlink_configured === false
      ? "Connect Microlink in Settings to use this scan."
      : scanLaunchConflict("microlink") || "Scan request in progress.";
    if (lane) {
      for (const control of lane.form.elements) control.disabled = locked;
      startButton.disabled = locked || !approval;
    }
  }
  function message(text = "") {
    status.textContent = text;
    status.hidden = !text;
  }
  function reset() {
    approval = null;
    message();
    update();
  }
  function select(trigger) {
    mode = trigger.dataset.scanSetup || "smart";
    activeDialog = laneDialogs[mode] || dialog;
    const actions = $(".scan-setup-actions", activeDialog);
    actions.append(startButton);
    $(".scan-setup-footer", activeDialog).prepend(status);
    $(".scan-setup-body", activeDialog).prepend(laneLockNote);
    dialog.dataset.scanMode = "smart";
    syncScanGoalHelper(dialog, mode);
    $("#smart-scan-options-title", dialog).textContent = "Smart Scan";
    $("#smart-scan-options-subtitle", dialog).textContent = descriptions[mode];
    for (const panel of panels) panel.hidden = false;
    methods.open = true;
    showScanModes();
    const lane = lanes[mode];
    smartButton.hidden = false;
    startButton.hidden = !lane;
    startButton.textContent = lane?.startLabel || "";
    reset();
  }
  dialog.addEventListener("scan-goal-applied", () => {
    select($("#scan-launch-trigger", root));
  });
  async function perform(work) {
    pending = true;
    update();
    try { await work(); }
    catch (error) { message(error.message); }
    finally { pending = false; update(); }
  }

  for (const trigger of triggers) {
    trigger.addEventListener("click", () => {
      if (trigger.hidden || trigger.disabled || pending || dialog.open) return;
      select(trigger);
      activeDialog.showModal();
      if (mode === "smart") {
        refreshSelectedScanEstimate();
        void seedMissionScanTerms(dialog).catch(error => { $("#scan-goal-status", dialog).textContent = error.message; });
      } else scanEstimateRefresh.get(lanes[mode].form)?.();
      $(".scan-setup-body", activeDialog).scrollTop = 0;
      root.dispatchEvent(new Event("microlink-refresh"));
    });
  }
  $("#retry-scan-inventory", root).addEventListener("click", () => void loadSmartScan());
  discoveryForm.addEventListener("input", event => {
    const {name, checked} = event.target;
    if (checked && name === "limit_to_ai") discoveryForm.elements.limit_to_com.checked = false;
    if (checked && name === "limit_to_com") discoveryForm.elements.limit_to_ai.checked = false;
  });
  discoveryForm.elements.lane.addEventListener("change", () => {
    const wasMission = scope === "mission";
    scope = discoveryForm.elements.lane.value;
    const mission = scope === "mission";
    const {request_limit, candidate_limit} = discoveryForm.elements;
    request_limit.max = mission ? "5000" : "160";
    candidate_limit.max = mission ? "5000" : "500";
    if (mission !== wasMission) {
      request_limit.value = mission ? "1000" : "24";
      candidate_limit.value = mission ? "1000" : "100";
    }
    reset();
  });
  for (const lane of Object.values(lanes)) {
    bindAutomaticScanEstimate(lane.form, async event => {
      if (blocked()) return;
      const quote = await requestScanEstimate(event, `${lane.monitor.endpoint}/estimate`, () => optionsFor(lane));
      if (!quote || blocked()) return;
      if (quote.configured && quote.max_requests) {
        approval = {options:optionsFor(lane), estimate_id:quote.estimate_id, confirmed_max_requests:quote.max_requests};
      }
      startButton.textContent = approval
        ? `${lane.startLabel} · up to ${formatNumber(quote.max_requests)} requests` : lane.startLabel;
      message(quote.configured ? lane.summary(quote) : "Configure Microlink in Settings.");
      update();
    }, () => {
      reset();
      startButton.textContent = lane.startLabel;
    });
  }
  startButton.addEventListener("click", () => {
    const lane = lanes[mode];
    if (!lane || !approval || blocked() || !lane.form.reportValidity()) return;
    if (JSON.stringify(optionsFor(lane)) !== JSON.stringify(approval.options)) {
      scanEstimateRefresh.get(lane.form)?.();
      return;
    }
    const {options, ...confirmation} = approval;
    reset();
    void perform(async () => {
      message("Confirming scan limits…");
      state.microlinkScanStarting = true;
      updateSmartScanLaunchControls();
      try {
        const result = await api(`${lane.monitor.endpoint}/runs`, {
          method: "POST", body: JSON.stringify({...options, ...confirmation}),
        });
        message();
        lane.monitor.started(result, activeDialog);
      } finally {
        state.microlinkScanStarting = false;
        updateSmartScanLaunchControls();
      }
    });
  });
  root.addEventListener("microlink-render", update);
  for (const lane of Object.values(lanes)) lane.monitor.onUpdate(update);
}

function bindScanLauncher() {
  const directoriesTrigger = $("#find-directories-trigger");
  directoriesTrigger.disabled = false;
  directoriesTrigger.addEventListener("click", () => {
    if (!$("#web-scan-form")) scanMethodsShell();
    const dialog = $("#find-directories-dialog");
    if (!dialog.open) dialog.showModal();
    void Promise.all([loadDiscoveryMethods(), loadIntelligence()]);
  });
  const trigger = $("#import-companies-trigger");
  trigger.addEventListener("click", () => {
    if (trigger.disabled || state.importScanStarting) return;
    setScanImportMode(true);
  });
  $("#start-smart-scan").addEventListener("click", () => {
    if (smartScanLaunchBlocked() || $("#start-smart-scan").disabled) return;
    void startSmartScan();
  });
}

function smartScanViewShell() {
  $("#view-smart_scan").innerHTML = `
    <div id="scan-smart-mode">${smartScanShellMarkup()}</div>
    <dialog class="modal smart-scan-options-dialog standalone-scan-dialog" id="import-companies-dialog" data-scan-mode="import" aria-labelledby="import-companies-title">
      <header class="modal-head"><h2 id="import-companies-title" tabindex="-1" autofocus>Import companies</h2><button type="button" class="icon-button" data-close-dialog="import-companies-dialog" aria-label="Close import companies">×</button></header>
    <form id="import-scan-form" class="modal-body scan-setup-body">
      <div class="paid-form-body">
        <label class="field"><span>Company URLs · up to 10,000 per import</span>
          <textarea name="urls" rows="12" required spellcheck="false" autocapitalize="off"
            placeholder="https://company.com&#10;another-company.com" aria-describedby="import-scan-count"></textarea>
        </label>
        <label class="field"><span>Tag (optional)</span><input name="tag" maxlength="40" placeholder="e.g. october prospects"></label>
        <small id="import-scan-count">0 entries · separate with lines, spaces or commas</small>
        <div id="import-scan-result" role="status" aria-live="polite"></div>
        <div id="import-scan-progress" role="status" aria-live="polite"></div>
      </div>
      <footer class="paid-form-actions"><button class="button" type="button" id="cancel-import-scan">Cancel</button><button class="button primary" type="submit" id="start-import-scan">Import and scan · $0.00</button></footer>
    </form></dialog>`;
  renderScanLauncherStatus();
  void pollScanStatus();
  installScanPaneDivider($("#smart-scan-shell"));
  $("#cancel-import-scan").addEventListener("click", () => setScanImportMode(false));
  $("#import-scan-form").addEventListener("submit", startImportScan);
  $("#import-scan-form").elements.urls.addEventListener("input", event => {
    const count = event.target.value.trim().split(/[\s,;]+/).filter(Boolean).length;
    $("#import-scan-count").textContent = `${formatNumber(count)} entries · separate with lines, spaces or commas`;
  });
}

function renderImportScanProgress() {
  const root = $("#import-scan-progress");
  if (!root || !state.importScanJobId) return;
  const run = state.webScanStatus;
  if (run?.job_id !== state.importScanJobId) return;
  const percent = discoveryProgressPercent(run);
  root.textContent = `${humanize(run.status || "running")} · ${run.message || "Scanning imported companies"}${percent != null ? ` · ${Number(percent.toFixed(1))}%` : ""}`;
}

async function startImportScan(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#start-import-scan");
  if (scanActionBlocked(button) || scanLaunchConflict("import") || !form.reportValidity()) return;
  const urls = form.elements.urls.value;
  const entries = urls.trim().split(/[\s,;]+/).filter(Boolean).length;
  const result = $("#import-scan-result");
  if (entries > 10000 || new TextEncoder().encode(urls).length > 4 * 1024 * 1024) {
    result.textContent = "Paste at most 10,000 URLs and 4 MiB per import; split larger lists.";
    return;
  }
  state.importScanStarting = true;
  updateSmartScanLaunchControls();
  button.disabled = true;
  form.elements.urls.disabled = true;
  button.textContent = "Importing…";
  result.textContent = "Saving companies…";
  try {
    const payload = await api("/api/scans/import", {method: "POST", body: JSON.stringify({urls, tag: form.elements.tag.value})});
    const counts = `${formatNumber(payload.created)} new · ${formatNumber(payload.existing)} existing · ${formatNumber(payload.duplicates)} duplicates · ${formatNumber(payload.invalid)} invalid · ${formatNumber(payload.blocked)} blocked`;
    result.textContent = counts;
    if (payload.errors?.length) {
      const errors = document.createElement("textarea");
      errors.readOnly = true;
      errors.rows = 3;
      errors.setAttribute("aria-label", "Skipped URL entries");
      errors.value = payload.errors.map(item => `Entry ${item.entry}: ${item.reason}`).join("\n");
      result.append(document.createElement("br"), errors);
    }
    if (payload.status === "not_started") {
      $("#import-scan-progress").textContent = payload.message + (payload.error ? ` ${payload.error}` : "");
      return;
    }
    state.importScanJobId = payload.job_id;
    state.webScanJobId = payload.job_id;
    state.scanStatus = payload.status;
    state.webScanStatus = payload;
    renderImportScanProgress();
    renderAggregateScanState();
    setScanImportMode(false);
    showToast(`Imported ${formatNumber(payload.accepted)} companies · scan started`);
    pollScanStatus();
  } catch (error) {
    result.textContent = error.message;
  } finally {
    state.importScanStarting = false;
    button.disabled = false;
    form.elements.urls.disabled = false;
    updateSmartScanLaunchControls();
  }
}

function webScanConfig() {
  const form = $("#web-scan-form");
  if (!form) return null;
  return {
    mission_id:selectedScanMission(),
    max_hosts_per_source: Number(form.elements.max_hosts_per_source.value),
    max_network_inspections: Number(form.elements.max_network_inspections.value),
    free_intelligence: form.elements.free_intelligence?.checked !== false,
    agency_discovery: form.elements.agency_discovery?.checked !== false,
    people_hydration_enabled: Boolean(form.elements.people_hydration_enabled?.checked),
    microlink_enabled: Boolean(form.elements.microlink_enabled?.checked),
  };
}

function invalidateWebScanEstimate() {
  if (!state.webScanEstimate) return;
  state.webScanEstimate = null;
  renderWebScanEstimate();
}

async function estimateWebScan(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (state.webScanStarting || scanControlLocks.has(form) || !form.checkValidity()) return;
  const estimate = await requestScanEstimate(event, "/api/web-scan/estimates", webScanConfig);
  if (!estimate) return;
  state.webScanEstimate = {...estimate, draft:JSON.stringify(webScanConfig())};
  renderWebScanEstimate();
}

function renderWebScanEstimate() {
  const root = $("#web-scan-quote");
  if (!root) return;
  const estimate = state.webScanEstimate;
  if (!estimate) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  root.hidden = false;
  root.innerHTML = `
    <div class="panel-head"><h3>Maximum work</h3><span class="badge moss">${estimate.provider_cost_usd == null ? "Usage caps" : maximumCostMoney(estimate.provider_cost_usd) + " max"}</span></div>
    <div class="web-scan-estimate">
      <details><summary>Work limits</summary><div class="web-scan-stats">
        <span><small>Enabled sources</small><strong>${formatNumber(estimate.enabled_source_count)}</strong></span>
        <span><small>Candidate pool</small><strong>${formatNumber(estimate.candidate_capacity)}</strong></span>
        <span><small>Websites</small><strong>${formatNumber(estimate.max_network_inspections)}</strong></span>
        <span><small>Page requests</small><strong>≤ ${formatNumber(estimate.max_page_requests)}</strong></span>
        <span><small>People companies</small><strong>≤ ${formatNumber(estimate.people_hydration?.company_count || 0)}</strong></span>
        ${estimate.has_unpriced_provider_usage ? `<span><small>Microlink fallbacks</small><strong>≤ ${formatNumber(estimate.provider_calls?.microlink_website_fallbacks || 0)}</strong></span>` : ""}
      </div>
      <div class="web-scan-fixed"><span>English only</span><span>No OpenAI</span><span>No Apify</span><span>90-day people lookup hold</span></div>
      ${estimate.has_unpriced_provider_usage ? `<small>Microlink usage is unpriced here; known people maximum ${maximumCostMoney(estimate.known_provider_cost_usd)}.</small>` : ""}
      </details>
      <button class="button primary web-scan-run" id="run-web-scan" type="button">${estimate.provider_cost_usd == null ? "Scan web · usage caps" : "Scan web · " + scanCapLabel(estimate) + " max"}</button>
    </div>`;
  $("#run-web-scan").addEventListener("click", runWebScan);
  syncGoButton();
}

async function runWebScan() {
  if (!state.webScanEstimate || scanActionBlocked($("#run-web-scan")) || state.webScanStarting || ACTIVE_RUN_STATES.has(state.scanStatus)) return;
  const form = $("#web-scan-form");
  if (scanControlLocks.has(form)) return;
  if (state.webScanEstimate.draft !== JSON.stringify(webScanConfig())) {
    scanEstimateRefresh.get(form)?.();
    return;
  }
  state.webScanStarting = true;
  setScanControlsLocked(form, true);
  syncGoButton();
  renderAggregateScanState();
  try {
    const payload = await api("/api/scans", { method: "POST", body: JSON.stringify(state.webScanEstimate.request) });
    state.scanStatus = "running";
    state.webScanJobId = payload.job_id || null;
    state.webScanStatus = { ...payload, method: "Web & CT", status: payload.status || "running" };
    renderAggregateScanState();
    showToast("Web scan started");
    pollScanStatus();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.webScanStarting = false;
    setScanControlsLocked(form, false);
    syncGoButton();
    renderAggregateScanState();
  }
}

function discoveryCapability(name) {
  const capabilities = state.discoveryMethods?.capabilities || {};
  const raw = capabilities[name] ?? capabilities[`${name}_configured`] ?? state.discoveryMethods?.[name];
  if (typeof raw === "boolean") return { ready: raw };
  if (raw && typeof raw === "object") {
    return {
      ...raw,
      ready: Boolean(firstValue(raw, ["ready", "configured", "available", "enabled"], false)),
    };
  }
  return { ready: false };
}

function discoveryTabReady(tab) {
  if (typeof tab.ready === "boolean") return tab.ready;
  if (tab.ready && typeof tab.ready === "object") return Boolean(firstValue(tab.ready, ["ready", "configured", "available", "enabled"], false));
  return true;
}

function discoveryRequirementLabels(tab) {
  const value = tab.requires;
  if (!value) return [];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.map(item => typeof item === "string" ? item : item?.label || item?.name || item?.id).filter(Boolean);
  return Object.entries(value).filter(([, required]) => Boolean(required)).map(([name]) => name);
}

function discoveryTabForSourceKind(sourceKind = "") {
  return discoveryTabs().find(tab => tab.kind === "method" && tab.source_kinds.includes(sourceKind)) || null;
}

function discoveryMethodRuns(tab) {
  const runs = Array.isArray(state.discoveryMethods?.recent_runs) ? state.discoveryMethods.recent_runs : [];
  return runs.filter(run => canonicalDiscoveryTabId(run.method, run.method_label) === tab.id);
}

function activeDiscoveryRuns() {
  const runs = Array.isArray(state.discoveryRuns) ? state.discoveryRuns : [];
  return runs.filter(run => ACTIVE_RUN_STATES.has(String(run?.status || "").toLowerCase()));
}

function activeDiscoveryRunsForTab(tab) {
  if (!tab) return [];
  return activeDiscoveryRuns().filter(run => canonicalDiscoveryTabId(run.method, run.method_label) === tab.id);
}

function activeDiscoveryRunForTab(tab) {
  return activeDiscoveryRunsForTab(tab)[0] || null;
}

function discoveryRunForTab(tab) {
  return activeDiscoveryRunForTab(tab) || discoveryMethodRuns(tab)[0] || null;
}

function discoveryRunnableSources(tab) {
  return (tab.sources || []).filter(source => (source.id ?? source.suggestion_id) != null
    && (!source.status || source.status === "accepted") && source.scan_eligible !== false);
}

function discoverySourceChoices(tab) {
  return discoveryRunnableSources(tab).map(source => {
    const identifier = source.id ?? source.suggestion_id;
    const secondary = source.apex || source.source_key || source.url || "Public source";
    return `<label class="discovery-source-choice"><input type="checkbox" name="suggestion_ids" value="${escapeHtml(identifier)}" ${identifier == null ? "disabled" : "checked"} /><span><strong>${escapeHtml(source.name || secondary)}</strong><small>${escapeHtml(secondary)} · ${escapeHtml(humanize(source.source_kind || "source"))}</small></span><b>${source.intelligence_score == null ? "" : escapeHtml(source.intelligence_score)}</b></label>`;
  }).join("");
}

function availableDiscoveryScanners(tab) {
  const activeScannerIds = new Set(activeDiscoveryRunsForTab(tab).flatMap(run => run.scanner_ids || run.request?.scanner_ids || []));
  return tab.scanners.filter(scanner => scanner.id && !activeScannerIds.has(scanner.id));
}

function discoveryScannerChoices(scanners) {
  return scanners.map(scanner => {
    return `<label class="discovery-source-choice"><input type="checkbox" name="scanner_ids" value="${escapeHtml(scanner.id)}" /><span><strong>${escapeHtml(humanize(String(scanner.id || "").replace(/_v\d+$/, "")))}</strong></span><b>${escapeHtml(humanize(scanner.cost_class || "public"))}</b></label>`;
  }).join("");
}

function syncDiscoveryProviderChoices(scan = state.smartScan || {}) {
  const profile = scan.profile || {};
  const configured = {
    people_hydration_enabled: scan.inventory?.people_hydration_configured ?? profile.people_hydration_configured,
    microlink_enabled: profile.microlink_configured,
  };
  for (const form of $$("#web-scan-form, [data-discovery-method-form]")) {
    if (scanControlLocks.has(form)) continue;
    for (const [name, available] of Object.entries(configured)) {
      const input = form.elements[name];
      if (!input) continue;
      input.disabled = available === false;
      if (input.disabled) input.checked = false;
      input.labels[0].hidden = input.disabled;
    }
  }
}

function discoveryPaidChoices() {
  return `<div class="paid-check-grid"><label class="paid-check"><input name="people_hydration_enabled" type="checkbox" /><span>People hydration · paid</span></label><label class="paid-check"><input name="microlink_enabled" type="checkbox" /><span>Microlink fallback · paid usage</span></label></div>`;
}

function discoveryGenericHiddenFields() {
  return `<input name="max_pages" type="hidden" value="1" /><input name="max_ai_tool_calls" type="hidden" value="1" /><input name="max_ai_reviews" type="hidden" value="0" /><input name="max_ai_web_reviews" type="hidden" value="0" /><input name="ai_resolution" type="checkbox" hidden disabled />`;
}

function discoveryMethodPanelMarkup(tab) {
  if (state.discoveryMethods?._error) {
    return `<section class="panel discovery-method-error"><strong>Method catalogue unavailable</strong><span>${escapeHtml(state.discoveryMethods._error)}</span><button class="button" type="button" data-retry-discovery-methods>Retry</button></section>`;
  }
  if (!state.discoveryMethods) return `<section class="panel discovery-method-loading"><p class="empty">${loadingIndicator(`Loading ${tab.label.toLowerCase()}…`)}</p></section>`;
  const ready = discoveryTabReady(tab);
  if (tab.id === "company_intelligence") {
    const scanners = availableDiscoveryScanners(tab);
    return `<div class="discovery-method-layout">
      <form class="panel discovery-method-form" data-discovery-method-form="${tab.id}" data-api-method="${escapeHtml(tab.api_id)}">
        ${discoveryMethodHeader(tab, `<span class="badge moss">90-day reuse</span>`)}
        <div class="discovery-method-body">
          <fieldset class="discovery-source-fieldset"><legend>Company scanners · ${formatNumber(scanners.length)} ${scanCardHelp("company-scanners", "Company scanners", "Choose which research checks to run on existing companies.")}</legend><div class="discovery-source-list">${discoveryScannerChoices(scanners)}</div></fieldset>
          <div class="discovery-limit-grid">
            <label class="field"><span>Current company cap</span><input name="max_candidates" type="number" min="1" max="500" value="100" required /></label>
            <label class="field"><span>Max website inspections</span><input name="max_network_inspections" type="number" min="1" max="500" value="100" required /></label>
          </div>
          ${discoveryGenericHiddenFields()}${discoveryPaidChoices()}
        </div>
      </form>
      <aside class="discovery-method-side"><section class="panel discovery-method-estimate" id="discovery-estimate-${tab.id}" aria-live="polite" hidden></section></aside>
    </div>`;
  }
  const sources = discoveryRunnableSources(tab);
  const needsSources = tab.source_kinds.length > 0;
  const requirements = discoveryRequirementLabels(tab);
  const openAI = discoveryCapability("openai");
  const aiRequired = requirements.some(item => String(item).toLowerCase().includes("openai")) || tab.id === "searches";
  const aiChecked = openAI.ready && (aiRequired || tab.id === "tenant_footprints");
  return `<div class="discovery-method-layout">
    <form class="panel discovery-method-form" data-discovery-method-form="${tab.id}" data-api-method="${escapeHtml(tab.api_id)}">
      ${discoveryMethodHeader(tab, `<span class="badge ${ready ? "moss" : "warn"}">${ready ? "Public-web crawl" : "Setup required"}</span>`)}
      <div class="discovery-method-body">
        ${needsSources ? `<fieldset class="discovery-source-fieldset"><legend>Sources ready · ${formatNumber(sources.length)} ${scanCardHelp(`sources-${tab.id}`, "Sources ready", "Choose the approved sources this scan should check.")}</legend><div class="discovery-source-list">${discoverySourceChoices(tab)}</div></fieldset>` : ""}
        <div class="discovery-limit-grid">
          <label class="field"><span>Max pages per source</span><input name="max_pages" type="number" min="1" max="50" value="50" required /></label>
          <label class="field"><span>Max candidates</span><input name="max_candidates" type="number" min="1" max="500" value="500" required /></label>
          <label class="field"><span>Max website inspections</span><input name="max_network_inspections" type="number" min="1" max="500" value="500" required /></label>
          <label class="field" ${aiChecked ? "" : "hidden"}><span>AI source web calls</span><input name="max_ai_tool_calls" type="number" min="1" max="10" value="${aiChecked ? "10" : "0"}" ${aiChecked ? "" : "disabled"} required /></label>
          <label class="field" ${aiChecked ? "" : "hidden"}><span>AI candidate reviews</span><input name="max_ai_reviews" type="number" min="0" max="50" value="${aiChecked ? "50" : "0"}" ${aiChecked ? "" : "disabled"} required /></label>
          <label class="field" ${aiChecked ? "" : "hidden"}><span>AI web reviews</span><input name="max_ai_web_reviews" type="number" min="0" max="20" value="${aiChecked ? "20" : "0"}" ${aiChecked ? "" : "disabled"} required /></label>
        </div>
        ${discoveryPaidChoices()}
        <label class="discovery-ai-toggle" ${openAI.ready && !aiRequired ? "" : "hidden"}><input name="ai_resolution" type="checkbox" ${aiChecked ? "checked" : ""} ${openAI.ready && !aiRequired ? "" : "disabled"} /><span><strong>AI resolution and reviews</strong></span></label>
        ${requirements.length ? `<p class="discovery-requirements">Requires ${requirements.map(item => escapeHtml(humanize(item))).join(" · ")}</p>` : ""}
        ${!ready ? `<p class="paid-run-error">${escapeHtml(tab.reason || tab.message || "This method is not ready. Review Settings for the missing capability.")}</p>` : ""}
      </div>
    </form>
    <aside class="discovery-method-side">
      <section class="panel discovery-method-estimate" id="discovery-estimate-${tab.id}" aria-live="polite" hidden></section>
    </aside>
  </div>`;
}

function bindDiscoveryMethodForms() {
  $$('[data-discovery-method-form]').forEach(form => {
    const invalidate = event => {
      const tabId = form.dataset.discoveryMethodForm;
      if (state.discoveryEstimates[tabId]) {
        delete state.discoveryEstimates[tabId];
        renderDiscoveryMethodEstimate(tabId);
      }
      const aiEnabled = form.elements.ai_resolution.checked;
      const toggledOn = aiEnabled && event?.target === form.elements.ai_resolution;
      const aiLimits = [
        [form.elements.max_ai_tool_calls, 10],
        [form.elements.max_ai_reviews, 50],
        [form.elements.max_ai_web_reviews, 20],
      ];
      aiLimits.forEach(([input, defaultValue]) => {
        if (!input) return;
        input.disabled = !aiEnabled;
        if (input.labels?.[0]) input.labels[0].hidden = !aiEnabled;
        if (!aiEnabled) input.value = "0";
        else if ((toggledOn && Number(input.value) === 0) || Number(input.value) < (input.name === "max_ai_tool_calls" ? 1 : 0)) input.value = String(defaultValue);
      });
    };
    form.addEventListener("input", invalidate);
    form.addEventListener("change", invalidate);
    bindAutomaticScanEstimate(form, estimateDiscoveryMethod, () => {
      delete state.discoveryEstimates[form.dataset.discoveryMethodForm];
      renderDiscoveryMethodEstimate(form.dataset.discoveryMethodForm);
    });
  });
  $$('[data-retry-discovery-methods]').forEach(button => button.addEventListener("click", loadDiscoveryMethods));
}

function renderDiscoveryMethodPanels() {
  discoveryTabs().filter(tab => tab.kind === "method").forEach(tab => {
    const root = $(`#discovery-method-${tab.id}`);
    if (root) root.innerHTML = discoveryMethodPanelMarkup(tab);
  });
  addScanFieldHelp($("#scan-methods-content"));
  bindDiscoveryMethodForms();
  syncDiscoveryProviderChoices();
  if (discoveryTabs().some(tab => tab.kind === "method" && tab.id === state.discoveryTab)) refreshSelectedScanEstimate();
  enhanceMultiChecklists($("#scan-methods-content"));
  renderDiscoveryMethodRuns();
}

function discoveryMethodRequest(form) {
  const tabId = form.dataset.discoveryMethodForm;
  const tab = discoveryTabs().find(item => item.id === tabId);
  if (!tab) throw new Error("Unknown discovery method");
  const suggestionIds = $$('input[name="suggestion_ids"]:checked', form).map(input => Number(input.value)).filter(Number.isFinite);
  const scannerIds = $$('input[name="scanner_ids"]:checked', form).map(input => input.value).filter(Boolean);
  const serverOwned = new Set(["public_graph", "company_intelligence"]);
  if (!serverOwned.has(tab.id) && !suggestionIds.length) throw new Error("Select at least one accepted source");
  if (tab.id === "company_intelligence" && !scannerIds.length) throw new Error("Select at least one company scanner");
  const aiResolution = Boolean(form.elements.ai_resolution.checked);
  const maxAIReviews = aiResolution ? Number(form.elements.max_ai_reviews.value) : 0;
  const maxAIWebReviews = aiResolution ? Number(form.elements.max_ai_web_reviews.value) : 0;
  if (maxAIWebReviews > maxAIReviews) throw new Error("AI web reviews cannot exceed total AI candidate reviews");
  return {
    mission_id:selectedScanMission(),
    method: tab.api_id,
    suggestion_ids: suggestionIds,
    scanner_ids: scannerIds,
    lookback_days: Number(form.elements.lookback_days?.value || 7),
    max_feed_items: Number(form.elements.max_feed_items?.value || 100),
    max_pages: Number(form.elements.max_pages.value),
    max_candidates: Number(form.elements.max_candidates.value),
    max_network_inspections: Number(form.elements.max_network_inspections.value),
    ai_resolution: aiResolution,
    people_hydration_enabled: Boolean(form.elements.people_hydration_enabled?.checked),
    microlink_enabled: Boolean(form.elements.microlink_enabled?.checked),
    // The server keeps a positive validation floor even when AI resolution is disabled.
    max_ai_tool_calls: aiResolution ? Number(form.elements.max_ai_tool_calls.value) : 1,
    max_ai_reviews: maxAIReviews,
    max_ai_web_reviews: maxAIWebReviews,
  };
}

async function estimateDiscoveryMethod(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (state.discoveryStarting || scanControlLocks.has(form) || !form.checkValidity()) return;
  const tabId = form.dataset.discoveryMethodForm;
  const request = discoveryMethodRequest(form);
  const payload = await requestScanEstimate(event, "/api/discovery-methods/estimates", () => discoveryMethodRequest(form));
  if (!payload) return;
  const estimate = payload.estimate || payload;
  state.discoveryEstimates[tabId] = {payload, request:estimate.request || request, fingerprint:JSON.stringify(request)};
  renderDiscoveryMethodEstimate(tabId);
}

function renderDiscoveryMethodEstimate(tabId, error = "") {
  const root = $(`#discovery-estimate-${tabId}`);
  if (!root) return;
  const stored = state.discoveryEstimates[tabId];
  if (!stored) {
    root.hidden = !error;
    root.innerHTML = error ? `<div class="discovery-method-empty error"><strong>Estimate unavailable</strong><span>${escapeHtml(error)}</span></div>` : "";
    return;
  }
  root.hidden = false;
  const estimate = stored.payload.estimate || stored.payload;
  const warnings = Array.isArray(estimate.warnings) ? estimate.warnings : (estimate.warnings ? [estimate.warnings] : []);
  const blockers = Array.isArray(estimate.blockers) ? estimate.blockers : (estimate.blockers ? [estimate.blockers] : []);
  const omittedSourceCount = Number(estimate.omitted_source_count || 0);
  const sourceCount = Number(firstValue(estimate, ["source_count", "sources_total", "request.suggestion_ids.length"], stored.request.suggestion_ids.length));
  const candidateCap = Number(firstValue(estimate, ["max_candidates", "candidate_capacity", "request.max_candidates"], stored.request.max_candidates));
  const pageCap = Number(firstValue(estimate, ["max_pages", "maximum_pages", "request.max_pages"], stored.request.max_pages));
  const maxPageRequests = Number(firstValue(estimate, ["max_page_requests", "maximum_page_requests"], sourceCount * pageCap));
  const networkInspections = Number(firstValue(estimate, ["max_network_inspections", "request.max_network_inspections"], stored.request.max_network_inspections));
  const providerCalls = estimate.provider_calls || {};
  const providerCaps = {
    sourceResolutions: Number(providerCalls.openai_source_resolutions || 0),
    sourceWebCalls: Number(providerCalls.openai_source_web_calls || 0),
    candidateReviews: Number(providerCalls.openai_candidate_reviews || 0),
    webReviews: Number(providerCalls.openai_web_reviews || 0),
    apify: Number(providerCalls.apify || 0),
  };
  const usesOpenAI = providerCaps.sourceResolutions + providerCaps.sourceWebCalls + providerCaps.candidateReviews + providerCaps.webReviews > 0;
  const runCap = scanCapLabel(estimate);
  const unpriced = estimate.has_unpriced_provider_usage || estimate.provider_cost_usd == null;
  const knownMaximum = Number(estimate.known_provider_cost_usd || 0);
  const costBadge = unpriced ? "Provider usage caps" : Number(estimate.provider_cost_usd) > 0
    ? maximumCostMoney(estimate.provider_cost_usd) + " max" : "$0.00 provider spend";
  const runnable = estimate.runnable !== false && blockers.length === 0;
  const activeRunBlocksStart = tabId !== "company_intelligence" && Boolean(activeDiscoveryRunForTab(discoveryTabs().find(tab => tab.id === tabId)));
  root.innerHTML = `<div class="panel-head"><h3>Bounded estimate</h3><span class="badge ${usesOpenAI ? "" : "moss"}">${escapeHtml(costBadge)}</span></div><div class="discovery-method-estimate-body">
    <details><summary>Work and provider limits</summary><p class="discovery-estimate-kicker">Direct public work</p>
    <div class="web-scan-stats"><span><small>Sources</small><strong>${formatNumber(sourceCount)}</strong></span><span><small>Candidate cap</small><strong>${formatNumber(candidateCap)}</strong></span><span><small>Page requests</small><strong>≤ ${formatNumber(maxPageRequests)}</strong></span><span><small>Websites</small><strong>≤ ${formatNumber(networkInspections)}</strong></span></div>
    <p class="discovery-estimate-kicker">Provider call caps</p>
    ${knownMaximum > 0 ? `<small>Known people maximum ${maximumCostMoney(knownMaximum)}</small>` : ""}
    ${unpriced ? `<small>Total dollars unknown; provider usage is capped below.</small>` : ""}
    <div class="discovery-provider-calls"><span><small>Source resolutions</small><strong>≤ ${formatNumber(providerCaps.sourceResolutions)}</strong></span><span><small>Source web calls</small><strong>≤ ${formatNumber(providerCaps.sourceWebCalls)}</strong></span><span><small>Candidate reviews</small><strong>≤ ${formatNumber(providerCaps.candidateReviews)}</strong></span><span><small>Web reviews</small><strong>≤ ${formatNumber(providerCaps.webReviews)}</strong></span><span><small>Apify</small><strong>${formatNumber(providerCaps.apify)}</strong></span><span><small>People searches</small><strong>≤ ${formatNumber(providerCalls.treg_people_searches || 0)}</strong></span><span><small>Microlink fallbacks</small><strong>≤ ${formatNumber((providerCalls.microlink_source_pages || 0) + (providerCalls.microlink_website_fallbacks || 0))}</strong></span></div>
    ${estimate.cost_note ? `<p class="discovery-estimate-note">${escapeHtml(estimate.cost_note)}</p>` : ""}
    ${omittedSourceCount ? `<p class="discovery-estimate-note"><strong>${formatNumber(omittedSourceCount)} fresh source${omittedSourceCount === 1 ? "" : "s"} omitted automatically.</strong> Only due work is included.</p>` : ""}
    </details>
    ${warnings.length ? `<ul class="paid-warnings">${warnings.map(item => `<li>${escapeHtml(typeof item === "string" ? item : item.message || JSON.stringify(item))}</li>`).join("")}</ul>` : ""}
    ${blockers.length ? `<ul class="paid-warnings">${blockers.map(item => `<li>${escapeHtml(typeof item === "string" ? item : item.message || JSON.stringify(item))}</li>`).join("")}</ul>` : ""}
    ${error ? `<p class="paid-run-error">${escapeHtml(error)}</p>` : ""}
    <button class="button primary discovery-method-run" type="button" data-start-discovery-method="${tabId}" ${!runnable || state.discoveryStarting || activeRunBlocksStart ? "disabled" : ""}>Scan ${escapeHtml(discoveryTabs().find(tab => tab.id === tabId)?.label || "scan")} ${unpriced ? "· usage caps" : "· " + runCap + " max"}</button>
  </div>`;
  root.querySelector("[data-start-discovery-method]")?.addEventListener("click", startDiscoveryMethod);
}

async function startDiscoveryMethod(event) {
  const tabId = event.currentTarget.dataset.startDiscoveryMethod;
  const stored = state.discoveryEstimates[tabId];
  const form = $(`[data-discovery-method-form="${tabId}"]`);
  if (!stored || !form || state.discoveryStarting || scanActionBlocked(event.currentTarget) || scanControlLocks.has(form)) return;
  if (tabId !== "company_intelligence" && activeDiscoveryRunForTab(discoveryTabs().find(tab => tab.id === tabId))) {
    showToast("This scan method is already scanning", "info");
    return;
  }
  let current;
  try { current = discoveryMethodRequest(form); }
  catch (error) { showToast(error.message, "error"); return; }
  if (JSON.stringify(current) !== stored.fingerprint) {
    delete state.discoveryEstimates[tabId];
    renderDiscoveryMethodEstimate(tabId);
    scanEstimateRefresh.get(form)?.();
    return;
  }
  state.discoveryStarting = true;
  setScanControlsLocked(form, true);
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = "Starting…";
  let errorMessage = "";
  try {
    const payload = await api("/api/discovery-methods/runs", { method: "POST", body: JSON.stringify(stored.request) });
    state.discoveryRun = payload.run || payload.active_run || payload;
    const newRunIdentity = String(state.discoveryRun.run_id || state.discoveryRun.id || state.discoveryRun.job_id);
    const priorRuns = activeDiscoveryRuns().filter(run => String(run.run_id || run.id || run.job_id) !== newRunIdentity);
    state.discoveryRuns = tabId === "company_intelligence"
      ? [...priorRuns, state.discoveryRun]
      : [...priorRuns.filter(run => canonicalDiscoveryTabId(run.method, run.method_label) !== tabId), state.discoveryRun];
    if (tabId === "company_intelligence") {
      delete state.discoveryEstimates[tabId];
      renderDiscoveryMethodPanels();
    } else {
      renderDiscoveryMethodRuns();
    }
    renderDiscoveryTabs();
    showToast(`${discoveryTabs().find(tab => tab.id === tabId)?.label || "Scan"} scan started`);
    pollDiscoveryMethodStatus();
  } catch (error) {
    errorMessage = error.message;
    showToast(error.message, "error");
  } finally {
    state.discoveryStarting = false;
    setScanControlsLocked(form, false);
    renderDiscoveryMethodEstimate(tabId, errorMessage);
  }
}

function normalizeDiscoveryRun(payload = {}) {
  return payload.active_run || payload.run || payload.latest_run || (payload.status ? payload : null);
}

function normalizeDiscoveryRuns(payload = {}) {
  if (Array.isArray(payload.active_runs)) return payload.active_runs;
  const run = normalizeDiscoveryRun(payload);
  return run && ACTIVE_RUN_STATES.has(String(run.status || "").toLowerCase()) ? [run] : [];
}

function discoveryProgressPercent(run) {
  if (!run) return null;
  const explicit = Number(firstValue(run, ["progress_percent", "progress.percent"], NaN));
  if (Number.isFinite(explicit)) return Math.max(0, Math.min(100, explicit));
  const total = Number(firstValue(run, ["sources_total", "queries_total", "total", "progress.queries_total", "progress.total"], 0));
  const completed = Number(firstValue(run, ["sources_completed", "queries_completed", "completed", "processed", "progress.queries_completed", "progress.completed"], 0));
  return total > 0 ? Math.max(0, Math.min(100, completed / total * 100)) : null;
}

function discoveryErrorSummary(error) {
  if (!error) return "";
  const entries = String(error).split(/;\s*(?=[^;]*https?:\/\/)/);
  const hosts = new Set();
  const other = [];
  let throttled = 0;
  for (const entry of entries) {
    if (/HTTP 429|ProviderRateLimitDeferred/.test(entry)) {
      throttled += 1;
      const urls = entry.match(/https?:\/\/[^\s;]+/g) || [];
      for (const url of urls) {
        try { hosts.add(new URL(url).hostname); } catch (_) { /* Keep the error below. */ }
      }
    } else if (entry.trim()) other.push(entry.trim());
  }
  if (!throttled) return String(error);
  const target = hosts.size ? [...hosts].join(", ") : "Source";
  return [`${target}: rate limited (HTTP 429). Remaining pages stay due for retry.`, ...new Set(other)].join(" ");
}

function discoveryRunMarkup(run, { compact = false } = {}) {
  if (!run) return "";
  const status = String(run.status || "unknown").toLowerCase();
  const total = Number(firstValue(run, ["sources_total", "progress.total", "total"], 0));
  const completed = Number(firstValue(run, ["sources_completed", "progress.completed", "processed"], 0));
  const terminal = ["complete", "completed", "partial", "failed", "cancelled", "canceled"].includes(status);
  const rawPercent = Number(firstValue(run, ["progress_percent", "progress.percent"], total ? completed / total * 100 : ACTIVE_RUN_STATES.has(status) ? 4 : 100));
  const percent = terminal ? 100 : Math.max(0, Math.min(100, Number.isFinite(rawPercent) ? rawPercent : 0));
  const candidates = Number(firstValue(run, ["candidates_persisted", "candidates", "summary.candidates_persisted", "summary.candidates"], 0));
  const hydrated = Number(firstValue(run, ["candidates_hydrated", "hydrated", "summary.candidates_hydrated"], 0));
  const pages = Number(firstValue(run, ["pages_fetched", "summary.pages_fetched"], 0));
  const documents = Number(firstValue(run, ["documents_fetched", "summary.documents_fetched"], 0));
  const error = discoveryErrorSummary(run.error || run.summary?.error);
  const stats = [];
  if (total || completed) stats.push(`<span><strong>${formatNumber(completed)}</strong> / ${formatNumber(total || completed)} sources</span>`);
  if (pages) stats.push(`<span><strong>${formatNumber(pages)}</strong> ${pages === 1 ? "page" : "pages"}</span>`);
  if (documents) stats.push(`<span><strong>${formatNumber(documents)}</strong> ${documents === 1 ? "document" : "documents"}</span>`);
  if (candidates) stats.push(`<span><strong>${formatNumber(candidates)}</strong> candidates</span>`);
  if (hydrated) stats.push(`<span><strong>${formatNumber(hydrated)}</strong> hydrated</span>`);
  const message = !ACTIVE_RUN_STATES.has(status) && run.message && run.message !== error ? run.message : "";
  const scannerIds = run.scanner_ids || run.request?.scanner_ids || [];
  const scannerLabel = scannerIds.length ? `${scannerIds.slice(0, 2).map(id => humanize(String(id).replace(/_v\d+$/, ""))).join(", ")}${scannerIds.length > 2 ? ` +${scannerIds.length - 2}` : ""}` : "";
  const phaseLabel = `${scannerLabel ? `${scannerLabel} · ` : ""}${humanize(run.phase || status)}${run.started_at ? ` · ${relativeTime(run.started_at)}` : ""}`;
  const context = scannerLabel || !compact ? `<span class="section-meta">${escapeHtml(phaseLabel)}</span>` : "";
  return `<section class="panel discovery-run-card ${escapeHtml(status)} ${compact ? "compact" : ""}"><div class="panel-head"><div><h3>${escapeHtml(humanize(run.method || "Scan method"))}</h3>${context}</div><span class="badge ${status === "failed" ? "warn" : ACTIVE_RUN_STATES.has(status) ? "moss" : ""}">${escapeHtml(humanize(status))}</span></div><div class="paid-progress-body">
    <div class="paid-progress-track" role="progressbar" aria-label="Scan progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(0)}"><span style="width:${percent}%"></span></div>
    ${stats.length ? `<div class="discovery-run-stats">${stats.join("")}</div>` : ""}
    ${message ? `<p class="paid-run-message">${escapeHtml(message)}</p>` : ""}${error ? `<p class="paid-run-error">${escapeHtml(error)}</p>` : ""}
  </div></section>`;
}

function discoveryRunsMarkup(tab) {
  const active = activeDiscoveryRunsForTab(tab);
  if (active.length) return active.map(run => discoveryRunMarkup(run, { compact: true })).join("");
  const latest = discoveryMethodRuns(tab)[0];
  return latest ? discoveryRunMarkup(latest, { compact: true }) : "";
}

function renderDiscoveryMethodRuns() {
  discoveryTabs().filter(tab => tab.kind === "method").forEach(tab => {
    const root = $(`#discovery-run-${tab.id}`);
    if (root) root.innerHTML = discoveryRunsMarkup(tab);
  });
}

function discoveryOptionAvailable(tab, running) {
  if (tab.id === "paid_ads") {
    const capabilityKnown = state.adDiscoveryOverview || discoveryCapability("apify").ready
      || state.discoveryMethods?.capabilities?.apify === false;
    return running || !capabilityKnown || adDiscoveryTokenConfigured();
  }
  if (!discoveryTabReady(tab)) return running;
  if (!state.discoveryMethods || state.discoveryMethods._error) return true;
  if (tab.id === "company_intelligence") return availableDiscoveryScanners(tab).length > 0;
  return !tab.source_kinds.length || discoveryRunnableSources(tab).length > 0;
}

function renderDiscoveryTabs() {
  const tabs = discoveryTabs();
  const pending = Number(state.intelligence?.counts?.pending || 0);
  const paidActive = adDiscoveryRunActive();
  const scoutActive = state.intelligence?.scout_status?.status === "running";
  tabs.forEach(tab => {
    const button = $(`#discovery-tab-${tab.id}`);
    const meta = $(`#discovery-tab-meta-${tab.id}`);
    const failure = $(`#discovery-tab-failure-${tab.id}`);
    const progress = $(`#discovery-tab-progress-${tab.id}`);
    if (!button || !meta) return;
    const methodRuns = tab.kind === "method" ? activeDiscoveryRunsForTab(tab) : [];
    const methodRun = methodRuns[0] || null;
    const tabRun = tab.id === "web_ct" ? state.webScanStatus : tab.id === "paid_ads" ? state.adDiscoveryRun : methodRun;
    const running = Boolean(methodRun);
    const tabRunning = running || (tab.id === "web_ct" && ACTIVE_RUN_STATES.has(String(state.webScanStatus?.status || "").toLowerCase())) || (tab.id === "paid_ads" && paidActive) || (tab.id === "find_sources" && scoutActive);
    const ready = discoveryTabReady(tab);
    button.hidden = !discoveryOptionAvailable(tab, tabRunning);
    button.classList.toggle("running", tabRunning);
    button.classList.toggle("needs-setup", !ready);
    meta.textContent = tabRunning
      ? methodRuns.length > 1 ? `${formatNumber(methodRuns.length)} scanning` : "Scanning"
      : tab.kind === "method"
        ? ready
          ? tab.id === "company_intelligence"
            ? `${formatNumber(tab.scanners.length)} scanners`
            : `${formatNumber(discoveryRunnableSources(tab).length)} source${discoveryRunnableSources(tab).length === 1 ? "" : "s"}`
          : "Setup"
        : tab.id === "find_sources"
          ? `${formatNumber(pending)} pending`
          : tab.id === "paid_ads"
            ? adDiscoveryTokenConfigured() ? "Ready" : "Setup"
            : "Ready";
    const reliability = discoveryReliability(tab);
    if (failure) {
      failure.textContent = reliability.attempts
        ? `${reliability.rate.toFixed(1)}% failed · ${formatNumber(reliability.failures)} / ${formatNumber(reliability.attempts)} scans`
        : "No scans yet";
      failure.classList.toggle("high", reliability.attempts > 0 && reliability.rate >= 50);
    }
    if (progress) {
      const percent = discoveryProgressPercent(tabRun);
      progress.setAttribute("aria-hidden", String(!tabRunning));
      progress.classList.toggle("indeterminate", tabRunning && percent == null);
      if (percent == null) progress.removeAttribute("aria-valuenow");
      else progress.setAttribute("aria-valuenow", percent.toFixed(0));
      const fill = progress.querySelector("i");
      if (fill) fill.style.width = percent == null ? "38%" : `${percent}%`;
    }
  });
  const selected = state.discoveryTab;
  selectDiscoveryTab(selected);
  if (selected !== state.discoveryTab) refreshSelectedScanEstimate();
  renderAggregateScanState();
}

async function loadDiscoveryMethods() {
  if (!$("#discovery-tab-web_ct")) return;
  try {
    const hadCatalogue = Boolean(state.discoveryMethods && !state.discoveryMethods._error);
    state.discoveryMethods = await api("/api/discovery-methods?compact=true");
    if (hadCatalogue) state.discoveryEstimates = {};
    state.discoveryRuns = normalizeDiscoveryRuns(state.discoveryMethods);
    state.discoveryRun = state.discoveryRuns[0] || normalizeDiscoveryRun({ active_run: state.discoveryMethods.active_run }) || state.discoveryRun;
    renderDiscoveryMethodPanels();
    renderDiscoveryTabs();
    if (activeDiscoveryRuns().length) pollDiscoveryMethodStatus();
  } catch (error) {
    state.discoveryMethods = { tabs: [], capabilities: {}, recent_runs: [], _error: error.message };
    renderDiscoveryMethodPanels();
    renderDiscoveryTabs();
  }
}

async function pollDiscoveryMethodStatus() {
  if (state.discoveryPollInFlight || !["smart_scan", "sources"].includes(state.activeView)) return;
  clearTimeout(state.discoveryPollTimer);
  state.discoveryPollInFlight = true;
  try {
    const payload = await api("/api/discovery-methods/status");
    const previousRuns = activeDiscoveryRuns();
    state.discoveryRuns = normalizeDiscoveryRuns(payload);
    state.discoveryRun = state.discoveryRuns[0] || normalizeDiscoveryRun(payload);
    renderDiscoveryMethodRuns();
    renderDiscoveryTabs();
    if (activeDiscoveryRuns().length) {
      if (state.discoveryRuns.length < previousRuns.length) await loadDiscoveryMethods();
      if (state.activeView === "sources") await loadSources();
      state.discoveryPollTimer = setTimeout(pollDiscoveryMethodStatus, 2500);
    } else {
      if (previousRuns.length) {
        showToast(state.discoveryRun?.message || `Scan ${state.discoveryRun?.status || "finished"}`, scanToastTone(state.discoveryRun?.status));
        await loadDiscoveryMethods();
      }
      await refreshVisibleData();
    }
  } catch (error) {
    const visible = discoveryTabs().find(tab => tab.id === state.discoveryTab)?.kind === "method";
    if (visible) {
      const root = $(`#discovery-run-${state.discoveryTab}`);
      if (root) root.innerHTML = `<section class="panel discovery-method-error"><strong>Status temporarily unavailable</strong><span>${escapeHtml(error.message)}</span></section>`;
    }
    state.discoveryPollTimer = setTimeout(pollDiscoveryMethodStatus, 4000);
  } finally {
    state.discoveryPollInFlight = false;
  }
}

function sourceScoutAvailable(data = state.intelligence) {
  return typeof data?.source_scout_available === "boolean"
    ? data.source_scout_available
    : Boolean(state.dashboard?.capabilities?.source_scout);
}

async function startSourceScout(event) {
  event.preventDefault();
  if (state.sourceScoutStarting || state.intelligence?.scout_status?.status === "running") return;
  if (!sourceScoutAvailable()) {
    showToast("Add an OpenAI API key in Settings before finding more sources", "error");
    syncSourceScoutControls();
    return;
  }
  const useDefaults = event.currentTarget.dataset.sourceScoutStart === "defaults";
  const form = event.currentTarget.matches("[data-source-scout-form]")
    ? event.currentTarget
    : event.currentTarget.closest("[data-source-scout-form]");
  if (!useDefaults && (!form || !form.reportValidity())) return;
  const request = useDefaults
    ? {}
    : { max_suggestions: Number(form.elements.max_suggestions.value), max_tool_calls: Number(form.elements.max_tool_calls.value) };
  request.mission_id = selectedScanMission();
  state.sourceScoutStarting = true;
  syncSourceScoutControls();
  try {
    const payload = await api("/api/source-scouts", { method: "POST", body: JSON.stringify(request) });
    renderIntelligence({ ...(state.intelligence || {}), scout_status: payload });
    showToast(useDefaults ? "Finding more sources" : "Source scout started");
    pollSourceScout();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    state.sourceScoutStarting = false;
    syncSourceScoutControls();
  }
}

function syncSourceScoutControls() {
  const available = sourceScoutAvailable();
  const running = state.sourceScoutStarting || state.intelligence?.scout_status?.status === "running";
  $$('[data-source-scout-start]').forEach(button => {
    const usesDefaults = button.dataset.sourceScoutStart === "defaults";
    button.disabled = !available || running;
    button.setAttribute("aria-busy", String(running));
    button.textContent = state.sourceScoutStarting
      ? "Starting…"
      : running
        ? usesDefaults ? "Finding…" : "Scout scanning"
        : !available && !usesDefaults ? "OpenAI key required" : usesDefaults ? "Find More · AI" : "Find sources";
  });
  $$('[data-source-scout-form] input').forEach(input => { input.disabled = !available || running; });
  const availability = $("#source-scout-availability");
  if (availability) {
    availability.textContent = available ? "OpenAI ready" : "OpenAI key required";
    availability.className = `badge ${available ? "moss" : "warn"}`;
  }
}

function scanOverviewItems() {
  const rows = [];
  const add = (label, run, parent = false) => {
    const status = String(run?.status || "idle").toLowerCase();
    if (["idle", "ready", "unknown"].includes(status)) return;
    rows.push({ label, run, status, parent });
  };
  const live = state.scanOverview;
  const smart = state.activeView === "smart_scan" || state.smartScanStarting
    ? state.smartScan || live?.smart : live?.smart || state.smartScan;
  add("Smart Scan", smart, true);
  add(state.importScanJobId && state.webScanStatus?.job_id === state.importScanJobId ? "Import" : "Web & CT", state.webScanStarting ? {status:"starting"} : state.webScanStatus);
  const runs = [...(live?.methods || []), ...(state.discoveryRuns || []), ...(state.discoveryMethods?.recent_runs || [])];
  const seen = new Set();
  for (const run of runs) {
    const key = String(run.id || run.run_id || `${run.method}:${run.started_at}`);
    if (seen.has(key)) continue;
    seen.add(key);
    add(run.method_label || humanize(run.method || "Source scan"), run);
  }
  const stamp = run => run?.updated_at || run?.finished_at || run?.started_at || "";
  const latest = (local, remote) => stamp(local) >= stamp(remote) ? local || remote : remote;
  add("Paid ads", latest(state.adDiscoveryRun, live?.paid));
  add("Find sources", latest(state.intelligence?.scout_status, live?.scout));
  add("People", latest(state.peopleHydration?.run, live?.people));
  return rows;
}

function scanProgressUnit(phase) {
  return ({agency_discovery:"public sources", evergreen_discovery:"source ideas",
    archive_footprint:"archive checks", news_intelligence:"news checks",
    free_intelligence:"enriched"})[phase] || "inspected";
}

let standaloneScanFocus = null;
function chooseStandaloneScan(rows, previous = null) {
  if (rows.some(row => row.parent && smartScanActive(row.run))) return null;
  const live = rows.filter(row => !row.parent && ACTIVE_RUN_STATES.has(row.status));
  if (live.length === 1) return live[0];
  if (live.length > 1) return previous ? live.find(row => scanFocusKey(row) === scanFocusKey(previous)) || null : null;
  return previous ? rows.find(row => !row.parent && scanFocusKey(row) === scanFocusKey(previous)) || previous : null;
}
function scanFocusKey(row) {
  return `${row.label}:${row.run.id || row.run.run_id || row.run.job_id || row.run.started_at || "latest"}`;
}
function renderStandaloneScanWorkspace(rows) {
  const root = $("#smart-scan-shell");
  if (!root || root.dataset.initialLoading === "true") return;
  const previous = standaloneScanFocus;
  standaloneScanFocus = state.microlinkOverview ? null : chooseStandaloneScan(rows, previous);
  root.classList.toggle("has-standalone-scan", Boolean(standaloneScanFocus));
  let panel = $("#standalone-scan-progress", root);
  if (!standaloneScanFocus) {
    if (previous) { standaloneScanFocus = null; renderSmartScan(state.smartScan || {}); }
    return;
  }
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "standalone-scan-progress";
    panel.className = "standalone-scan-progress";
    $(".smart-scan-progress-column", root).append(panel);
  }
  const {label, run, status} = standaloneScanFocus;
  const busy = ACTIVE_RUN_STATES.has(status);
  if (busy && (!previous || scanFocusKey(previous) !== scanFocusKey(standaloneScanFocus))) {
    const dialog = $("#smart-scan-options-dialog");
    if (dialog?.open) dialog.close();
  }
  const total = Number(firstValue(run, ["total_candidates", "company_count", "sources_total", "queries_total", "total", "progress.total"], 0));
  const done = Number(firstValue(run, ["processed_companies", "processed", "sources_completed", "queries_completed", "completed", "progress.completed"], 0));
  const percent = discoveryProgressPercent(run) ?? (total > 0 ? Math.min(100, done / total * 100) : null);
  const phase = run.current_company || run.message || humanize(run.phase || status);
  const markup = `<div class="smart-scan-phase-head"><span>${escapeHtml(label)} · ${escapeHtml(humanize(status))}</span>${percent != null ? `<strong>${Number(percent.toFixed(1))}%</strong>` : ""}</div>
    <progress class="standalone-scan-track" max="100" ${percent != null ? `value="${percent}"` : ""} aria-label="${escapeHtml(label)} progress"></progress>
    <p class="standalone-scan-current" role="status">${escapeHtml(phase)}</p>
    ${total ? `<p class="smart-scan-note">${formatNumber(done)} / ${formatNumber(Math.max(total, done))} items processed</p>` : ""}`;
  if (panel.innerHTML !== markup) panel.innerHTML = markup;
  const errors = Array.isArray(run.errors) ? run.errors : run.error ? [run.error] : [];
  const events = Array.isArray(run.events) ? run.events : Array.isArray(run.logs) ? run.logs : [];
  const selected = state.smartScanTerminalTab === "errors" ? errors : events;
  const lines = selected.map(entry => smartScanTerminalLine(typeof entry === "string"
    ? {message:entry, level:state.smartScanTerminalTab === "errors" ? "error" : "info"}
    : {...entry, message:entry.message || entry.detail || phase}));
  if (!lines.length) lines.push(smartScanTerminalLine({message:state.smartScanTerminalTab === "errors"
    ? "No failures reported for this scan." : phase, level:"info"}));
  const terminal = $("#smart-terminal-lines", root);
  renderTerminalRows(terminal, lines, {follow:smartScanTerminalNearBottom(terminal)});
  highlightLatestTerminalActivity(terminal, busy && state.smartScanTerminalTab === "activity");
  $("#smart-terminal-error-count", root).textContent = formatNumber(run.error_total ?? errors.length);
  $("#smart-terminal-meta", root).textContent = label;
  $("#copy-smart-scan-errors", root).hidden = true;
  $("#smart-terminal-error-note", root).hidden = state.smartScanTerminalTab !== "errors";
  $("#smart-terminal-elapsed", root).textContent = smartScanElapsedLabel(run);
}

function scanPillSuccesses(run) {
  const count = paths => {
    const value = firstValue(run, paths, null);
    return value != null && Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : null;
  };
  const added = count(["new_companies_added", "summary.new_companies_added"]);
  const succeeded = count(["summary.success_count", "success_count"]);
  const saved = count(["candidates_persisted", "summary.candidates_persisted"]);
  const rows = [];
  if (succeeded != null) rows.push(`<span class="scan-overview-new-companies">${formatNumber(succeeded)} succeeded</span>`);
  if (added != null) rows.push(`<span class="scan-overview-new-companies">found ${formatNumber(added)} new ${added === 1 ? "company" : "companies"}</span>`);
  else if (saved != null) rows.push(`<span class="scan-overview-new-companies">${formatNumber(saved)} ${saved === 1 ? "company" : "companies"} saved</span>`);
  return rows.join("");
}

function scanOverviewPills(rows, focused = null) {
  const parent = rows.find(row => row.parent);
  if (state.activeView === "smart_scan") {
    if (state.microlinkOverview) return [state.microlinkOverview];
    if (focused) return [focused];
    if (!parent) return [];
  } else if (!parent || !smartScanActive(parent.run)) {
    return rows.filter(row => ACTIVE_RUN_STATES.has(row.status));
  }
  const sameId = (expected, actual) => expected != null && actual != null && String(expected) === String(actual);
  return [parent, ...rows.filter(({label, run}) =>
    (label === "Paid ads" && sameId(parent.run.paid_ads_run_id, run.id ?? run.run_id))
    || (label === "Web & CT" && sameId(parent.run.child_scan_job_id, run.job_id)))];
}

function renderAggregateScanState() {
  const rows = scanOverviewItems();
  renderStandaloneScanWorkspace(rows);
  renderScanLauncherStatus();
  const active = rows.filter(row => ACTIVE_RUN_STATES.has(row.status) || (row.parent && smartScanActive(row.run)));
  const root = $("#scan-state");
  if (root) {
    root.hidden = !active.length;
    root.innerHTML = `<span class="status-dot"></span> ${formatNumber(active.length)} ${active.length === 1 ? "scan active" : "scans active"}`;
  }
  const overview = $("#scan-overview");
  if (!overview) return;
  const visible = scanOverviewPills(rows, standaloneScanFocus);
  overview.hidden = !visible.length;
  const markup = visible.map(({ label, run, status, parent }) => {
    const issues = ["failed", "interrupted"].includes(status);
    const busy = ACTIVE_RUN_STATES.has(status) || (parent && smartScanActive(run));
    const percent = parent ? smartScanProgress(run) : discoveryProgressPercent(run);
    const progress = busy && percent != null ? `<progress max="100" value="${percent}" aria-label="${escapeHtml(label)} progress"></progress>` : "";
    const web = label === "Web & CT" || label === "Import";
    const detail = label === "People" ? [
      run.company_count != null ? `${formatNumber(run.processed_companies || 0)} / ${formatNumber(run.company_count)} companies` : "",
      run.current_company || run.message,
    ].filter(Boolean).join(" · ") : web && busy ? [
      scanTerminology((run.phase || "starting").replaceAll("_", " ")),
      run.total_candidates ? `${formatNumber(run.processed || 0)} / ${formatNumber(run.total_candidates)} ${scanProgressUnit(run.phase)}` : run.discovered != null || run.max_candidates ? `${formatNumber(run.discovered || 0)} found${run.max_candidates ? ` · up to ${formatNumber(run.max_candidates)}` : ""}` : "",
    ].filter(Boolean).join(" · ") : "";
    const outcomes = issues ? "" : scanPillSuccesses(run);
    const pillStatus = ["complete", "completed", "partial"].includes(status)
      ? outcomes ? "" : "Finished" : humanize(status);
    const cardTag = state.activeView === "smart_scan" ? "article" : "a";
    const cardLink = cardTag === "a" ? ' href="#smart_scan"' : "";
    const activityDot = parent && busy
      ? `<span class="scan-overview-orb${status === "paused" ? "" : " is-pulsing"}" aria-hidden="true"></span>` : "";
    return `<${cardTag}${cardLink} class="scan-overview-item ${issues ? "has-errors" : ""} ${busy ? "is-active" : ""}"><div class="scan-overview-content"><div class="scan-overview-identity">${activityDot}<strong title="${escapeHtml(label)}">${escapeHtml(label)}</strong>${pillStatus ? `<span class="scan-overview-status">${escapeHtml(pillStatus)}</span>` : ""}${busy && percent != null ? `<span class="scan-overview-percent">${Number(percent.toFixed(parent ? 2 : 1))}%</span>` : ""}</div>${detail || (parent && run.resumed_after_restart_at) ? `<div class="scan-overview-detail">${detail ? `<span title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>` : ""}${parent && run.resumed_after_restart_at ? `<span>Restart recovery</span>` : ""}</div>` : ""}${outcomes ? `<div class="scan-overview-outcomes">${outcomes}</div>` : ""}${progress}</div></${cardTag}>`;
  }).join("");
  if (overview.dataset.renderedMarkup !== markup) {
    overview.innerHTML = markup;
    overview.dataset.renderedMarkup = markup;
  }
}

function adDiscoveryConfig() {
  const form = $("#paid-discovery-form");
  if (!form) return null;
  const mode = form.elements.mode.value;
  const metaQueries = String(form.elements.meta_queries.value || "").split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (["discover", "both"].includes(mode) && !form.elements.meta_enabled.checked) {
    throw new Error("Company scan requires Meta to remain enabled");
  }
  if (["discover", "both"].includes(mode) && !metaQueries.length) {
    throw new Error("Add at least one Meta keyword mission");
  }
  if (metaQueries.length > 20) throw new Error("Use no more than 20 Meta keyword missions per scan");
  if (!form.elements.meta_enabled.checked && !form.elements.tiktok_library_enabled.checked && !form.elements.tiktok_top_enabled.checked) {
    throw new Error("Enable at least one advertising surface");
  }
  return {
    mission_id:selectedScanMission(),
    mode,
    meta_enabled: form.elements.meta_enabled.checked,
    meta_queries: metaQueries,
    country: String(form.elements.country.value || "US").trim().toUpperCase(),
    active_only: form.elements.active_only.checked,
    meta_ads_per_query: Number(form.elements.meta_ads_per_query.value),
    max_new_advertisers: Number(form.elements.max_new_advertisers.value),
    meta_ads_per_advertiser: Number(form.elements.meta_ads_per_advertiser.value),
    existing_target_limit: Number(form.elements.existing_target_limit.value),
    tiktok_library_enabled: form.elements.tiktok_library_enabled.checked,
    tiktok_ads_per_advertiser: Number(form.elements.tiktok_ads_per_advertiser.value),
    tiktok_top_enabled: form.elements.tiktok_top_enabled.checked,
    tiktok_top_ads_per_advertiser: Number(form.elements.tiktok_top_ads_per_advertiser.value),
    verify_landing_domains: form.elements.verify_landing_domains.checked,
    hydrate_websites: form.elements.hydrate_websites.checked,
    website_hydration_limit: Number(form.elements.website_hydration_limit.value),
    max_total_charge_usd: Number(form.elements.max_total_charge_usd.value),
  };
}

function syncAdDiscoveryForm() {
  const form = $("#paid-discovery-form");
  if (!form || scanControlLocks.has(form)) return;
  const mode = form.elements.mode.value;
  const discovers = ["discover", "both"].includes(mode);
  const hydratesExisting = ["hydrate", "both"].includes(mode);
  if (discovers) form.elements.meta_enabled.checked = true;
  const usesMetaLookup = form.elements.meta_enabled.checked && (discovers || hydratesExisting);
  form.elements.meta_enabled.disabled = discovers;
  form.elements.meta_queries.disabled = !discovers || !form.elements.meta_enabled.checked;
  form.elements.meta_ads_per_query.disabled = !usesMetaLookup;
  form.elements.max_new_advertisers.disabled = !discovers;
  form.elements.existing_target_limit.disabled = !hydratesExisting;
  if (!hydratesExisting) form.elements.existing_target_limit.value = "0";
  if (hydratesExisting && Number(form.elements.existing_target_limit.value) < 1) form.elements.existing_target_limit.value = "10";
  form.elements.tiktok_ads_per_advertiser.disabled = !form.elements.tiktok_library_enabled.checked;
  form.elements.tiktok_top_ads_per_advertiser.disabled = !form.elements.tiktok_top_enabled.checked;
  form.elements.website_hydration_limit.disabled = !form.elements.hydrate_websites.checked;
  for (const name of ["meta_queries", "meta_ads_per_query", "max_new_advertisers",
    "existing_target_limit", "tiktok_ads_per_advertiser", "tiktok_top_ads_per_advertiser", "website_hydration_limit"]) {
    const input = form.elements[name];
    if (input.labels?.[0]) input.labels[0].hidden = input.disabled;
  }
}

function handleAdDiscoveryDraftChange() {
  syncAdDiscoveryForm();
  if (!state.adDiscoveryEstimate) return;
  state.adDiscoveryEstimate = null;
  renderAdDiscoveryQuote();
}

function prepareMetaExistingBacklogDraft() {
  const form = $("#paid-discovery-form");
  const inventory = state.adDiscoveryOverview?.existing_target_inventory || {};
  const due = Math.max(0, Number(inventory.due_candidates || 0));
  if (!form || !due) return 0;
  form.elements.mode.value = "hydrate";
  form.elements.meta_enabled.checked = true;
  form.elements.existing_target_limit.value = String(Math.min(due, Number(inventory.maximum_per_run || 500), 500));
  form.elements.tiktok_library_enabled.checked = false;
  form.elements.tiktok_top_enabled.checked = false;
  form.elements.hydrate_websites.checked = false;
  form.elements.website_hydration_limit.value = "0";
  syncAdDiscoveryForm();
  state.adDiscoveryEstimate = null;
  renderAdDiscoveryQuote();
  return due;
}

function adDiscoveryTokenConfigured(source = state.adDiscoveryOverview) {
  const keys = ["apify.configured", "apify_token.configured", "credential.configured",
    "apify_configured", "apify_token_configured", "token_configured", "key_configured",
    "capabilities.apify"];
  return Boolean(firstValue(source || {}, keys, state.discoveryMethods?.capabilities?.apify ?? false));
}

function normalizeAdEstimate(payload = {}, draft = null) {
  const estimate = payload.estimate || payload.quote || payload;
  return { ...payload, ...estimate, quote_id: estimate.quote_id || payload.quote_id, _draft: draft };
}

function adEstimateCap(estimate = state.adDiscoveryEstimate) {
  return costFrom(estimate || {}, ["maximum_charge_usd", "hard_cap_usd", "max_total_charge_usd", "request.max_total_charge_usd"], ["approved_cap_microusd"], null);
}

function renderAdDiscoveryQuote() {
  const root = $("#paid-discovery-quote");
  if (!root) return;
  const estimate = state.adDiscoveryEstimate;
  if (!estimate) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  root.hidden = false;
  const rawLineItems = estimate.line_items || estimate.breakdown || [];
  const lineItems = Array.isArray(rawLineItems) ? rawLineItems : [];
  const rawWarnings = estimate.warnings || [];
  const warnings = [...(Array.isArray(rawWarnings) ? rawWarnings : [rawWarnings])];
  if (estimate.has_unpriced_platform_usage && !warnings.some(item => String(typeof item === "string" ? item : item?.message).toLowerCase().includes("platform usage"))) {
    warnings.unshift("Apify platform usage may be billed separately; the provider charge cap remains authoritative.");
  }
  const cap = adEstimateCap(estimate);
  const known = costFrom(estimate, ["estimated_known_cost_usd", "estimated_cost_usd", "known_cost_usd"], ["estimated_cost_microusd"], 0);
  const runnable = estimate.runnable !== false && Number.isFinite(cap) && adDiscoveryTokenConfigured()
    && !state.adDiscoveryStarting && !adDiscoveryRunActive() && !scanControlLocks.has($("#paid-discovery-form"));
  root.innerHTML = `
    <div class="panel-head"><div><h3>Cost estimate</h3><span class="section-meta">${estimate.expires_at ? `Expires ${escapeHtml(relativeTime(estimate.expires_at))}` : "Short-lived quote"}</span></div><span class="badge ${runnable ? "moss" : "warn"}">${runnable ? "Ready to approve" : !adDiscoveryTokenConfigured() ? "Token required" : cap == null ? "Cap unavailable" : "Scan active"}</span></div>
    <div class="paid-quote-body">
      <div class="paid-cost-total"><span>Provider hard cap</span><strong>${maximumCostMoney(cap)}</strong><small>Estimated known charge ${money(known)}${estimate.budget_limited ? " · limited by your budget" : ""}</small></div>
      <details><summary>Cost breakdown</summary><div class="paid-cost-breakdown">${lineItems.length ? lineItems.map(item => {
        const itemCost = costFrom(item, ["known_cost_usd", "result_cost_usd", "maximum_cost_usd", "cost_usd"], ["cost_microusd"], 0);
        const results = firstValue(item, ["conservative_max_results", "nominal_max_results", "max_results", "quantity"], null);
        const details = [item.actor_runs != null ? `${formatNumber(item.actor_runs)} actor scan${Number(item.actor_runs) === 1 ? "" : "s"}` : "", results == null ? "" : `up to ${formatNumber(results)} results`].filter(Boolean).join(" · ");
        const label = item.kind || item.stage || item.label || item.surface || "Provider work";
        const surface = item.kind && item.surface ? `${humanize(item.surface)} · ` : "";
        return `<div><span><strong>${escapeHtml(humanize(label))}</strong>${details ? `<small>${escapeHtml(surface + details)}</small>` : ""}</span><b>${money(itemCost)}</b></div>`;
      }).join("") : `<div><span><strong>Bounded provider work</strong><small>The server will enforce the approved total.</small></span><b>${maximumCostMoney(cap)}</b></div>`}</div>
      </details>
      ${(warnings.length || !runnable) ? `<ul class="paid-warnings">${!runnable ? `<li>Add an Apify token in Settings before scanning.</li>` : ""}${warnings.map(item => `<li>${escapeHtml(typeof item === "string" ? item : item.message || JSON.stringify(item))}</li>`).join("")}</ul>` : ""}
      <label class="paid-confirmation"><input id="approve-paid-cap" type="checkbox" ${runnable ? "" : "disabled"} /><span>I approve a maximum provider charge of <strong>${maximumCostMoney(cap)}</strong> for this manual scan.</span></label>
      <button class="button primary paid-run-button" id="start-paid-discovery" type="button" disabled>Scan · ${money(known)} est. · ${maximumCostMoney(cap)} max</button>
    </div>`;
  const approval = $("#approve-paid-cap");
  const runButton = $("#start-paid-discovery");
  approval?.addEventListener("change", () => { runButton.disabled = !runnable || !approval.checked || state.adDiscoveryStarting; });
  runButton?.addEventListener("click", startAdDiscovery);
}

async function estimateAdDiscovery(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (state.adDiscoveryStarting || adDiscoveryRunActive() || scanControlLocks.has(form) || !form.checkValidity()) return;
  const payload = await requestScanEstimate(event, "/api/ad-discovery/estimates", adDiscoveryConfig);
  if (!payload) return;
  state.adDiscoveryEstimate = normalizeAdEstimate(payload, adDiscoveryConfig());
  renderAdDiscoveryQuote();
}

async function startAdDiscovery() {
  const estimate = state.adDiscoveryEstimate;
  const approval = $("#approve-paid-cap");
  const form = $("#paid-discovery-form");
  if (!estimate?.quote_id || !approval?.checked || scanActionBlocked($("#start-paid-discovery"))
      || scanControlLocks.has(form) || state.adDiscoveryStarting || adDiscoveryRunActive()) return;
  if (JSON.stringify(estimate._draft) !== JSON.stringify(adDiscoveryConfig())
      || Date.parse(estimate.expires_at) <= Date.now()) {
    scanEstimateRefresh.get(form)?.();
    return;
  }
  const cap = adEstimateCap(estimate);
  if (!Number.isFinite(cap) || cap <= 0) { showToast("Provider maximum unavailable; estimate again", "error"); return; }
  state.adDiscoveryApprovedCap = cap;
  state.adDiscoveryStarting = true;
  setScanControlsLocked(form, true);
  setScanControlsLocked($("#paid-discovery-quote"), true);
  const button = $("#start-paid-discovery");
  if (button) { button.disabled = true; button.textContent = "Starting…"; }
  try {
    const payload = await api("/api/ad-discovery/runs", { method: "POST", body: JSON.stringify({ quote_id: estimate.quote_id, confirmed: true, confirmed_max_usd: cap }) });
    state.adDiscoveryEstimate = null;
    state.adDiscoveryRun = payload.run || payload;
    renderAdDiscoveryQuote();
    renderAdDiscoveryRun();
    showToast("Paid scan started");
    pollAdDiscoveryStatus();
  } catch (error) {
    showToast(error.message, "error");
    if (button) { button.disabled = false; button.textContent = `Scan with ${maximumCostMoney(cap)} cap`; }
  } finally {
    state.adDiscoveryStarting = false;
    setScanControlsLocked(form, false);
    setScanControlsLocked($("#paid-discovery-quote"), false);
    renderAdDiscoveryRun();
    renderAdDiscoveryQuote();
  }
}

function normalizeAdDiscoveryRun(payload = {}) {
  if (payload.run && typeof payload.run === "object") return { ...payload, ...payload.run };
  if (payload.active_run && typeof payload.active_run === "object") return { ...payload, ...payload.active_run };
  if (payload.latest_run && typeof payload.latest_run === "object") return { ...payload, ...payload.latest_run };
  return payload;
}

function adDiscoveryRunActive(run = state.adDiscoveryRun) {
  return ["queued", "starting", "running", "cancelling", "canceling"].includes(String(run?.status || "").toLowerCase());
}

function renderAdDiscoveryRun() {
  setScanControlsLocked($("#paid-discovery-form"), state.adDiscoveryStarting || adDiscoveryRunActive());
  const root = $("#paid-discovery-run");
  if (!root) return;
  const run = state.adDiscoveryRun;
  if (!run || !adDiscoveryRunActive(run)) {
    root.innerHTML = "";
    renderDiscoveryTabs();
    return;
  }
  const progress = run.progress || {};
  const completed = Number(firstValue(progress, ["queries_completed", "completed"], 0));
  const total = Number(firstValue(progress, ["queries_total", "total"], 0));
  const percent = total ? Math.min(100, completed / total * 100) : (adDiscoveryRunActive(run) ? 4 : 100);
  const actual = run.cost_unknown ? null : costFrom(run, ["actual_cost_usd", "provider_cost_usd"], ["actual_cost_microusd"], null);
  const cap = costFrom(run, ["approved_cap_usd", "maximum_charge_usd", "request.max_total_charge_usd"], ["approved_cap_microusd"], state.adDiscoveryApprovedCap || null);
  const status = String(run.status || "unknown").toLowerCase();
  const displayStatus = run.cancel_requested && adDiscoveryRunActive(run) ? "cancelling" : status;
  const runId = run.id || run.run_id;
  const error = run.error || run.summary?.error;
  root.innerHTML = `<section class="panel paid-run-card ${escapeHtml(status)}">
    <div class="panel-head"><div><h3>Paid scan ${runId ? `#${escapeHtml(runId)}` : ""}</h3><span class="section-meta">${escapeHtml(humanize(run.phase || displayStatus))}${run.started_at ? ` · ${escapeHtml(relativeTime(run.started_at))}` : ""}</span></div><div class="paid-run-head-actions"><span class="badge ${status === "failed" ? "warn" : adDiscoveryRunActive(run) ? "moss" : ""}">${escapeHtml(humanize(displayStatus))}</span>${adDiscoveryRunActive(run) && runId && !run.cancel_requested ? `<button class="button" id="cancel-paid-discovery" type="button">Cancel</button>` : ""}</div></div>
    <div class="paid-progress-body">
      <div class="paid-progress-track" role="progressbar" aria-label="Paid scan progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(0)}"><span style="width:${percent}%"></span></div>
      <div class="paid-progress-stats">
        <span><strong>${formatNumber(completed)}</strong> / ${formatNumber(total)} queries</span>
        <span><strong>${formatNumber(firstValue(progress, ["advertisers_found"], 0))}</strong> advertisers</span>
        <span><strong>${formatNumber(firstValue(progress, ["links_verified"], 0))}</strong> verified domains</span>
        <span><strong>${formatNumber(firstValue(progress, ["candidates_promoted"], 0))}</strong> candidates</span>
        <span><strong>${formatNumber(firstValue(progress, ["creatives_found"], 0))}</strong> creatives</span>
        <span><strong>${actual == null ? "Unknown" : money(actual)}</strong> / ${maximumCostMoney(cap)} cap</span>
      </div>
      ${run.message ? `<p class="paid-run-message">${escapeHtml(run.message)}</p>` : ""}
      ${error ? `<p class="paid-run-error">${escapeHtml(error)}</p>` : ""}
    </div>
  </section>`;
  $("#cancel-paid-discovery")?.addEventListener("click", cancelAdDiscovery);
  renderDiscoveryTabs();
}

async function cancelAdDiscovery(event) {
  const run = state.adDiscoveryRun;
  const runId = run?.id || run?.run_id;
  if (!runId || !window.confirm("Cancel this paid scan? Charges already incurred remain, but no new provider work will start.")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Cancelling…";
  try {
    const payload = await api(`/api/ad-discovery/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    state.adDiscoveryRun = normalizeAdDiscoveryRun(payload);
    renderAdDiscoveryRun();
    showToast("Cancellation requested");
    pollAdDiscoveryStatus();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Cancel";
    showToast(error.message, "error");
  }
}

function renderAdDiscoveryLinks() {
  const root = $("#paid-link-body");
  if (!root) return;
  const allLinks = state.adDiscoveryLinks || [];
  const links = state.adDiscoveryLinkFilter === "all" ? allLinks : allLinks.filter(item => item.state === state.adDiscoveryLinkFilter);
  const total = Math.max(Number(state.adDiscoveryLinkTotal || 0), allLinks.length);
  $("#paid-link-count").textContent = `${formatNumber(links.length)} shown · ${formatNumber(allLinks.length)} loaded · ${formatNumber(total)} total`;
  root.innerHTML = links.length ? links.map(item => {
    const advertiser = item.advertiser || {};
    const rawEvidence = item.evidence || [];
    const evidence = Array.isArray(rawEvidence) ? rawEvidence : [rawEvidence];
    const features = item.match_features || {};
    const profile = safeUrl(advertiser.profile_url || item.profile_url || "");
    const landing = safeUrl(item.landing_url || "");
    const evidenceLinks = evidence.filter(value => typeof value === "string" ? /^https?:/i.test(value) : value?.url || value?.source_url).slice(0, 3);
    return `<tr>
      <td class="source-name"><strong>${profile === "#" ? escapeHtml(advertiser.display_name || advertiser.handle || "Unknown advertiser") : `<a href="${profile}" target="_blank" rel="noopener">${escapeHtml(advertiser.display_name || advertiser.handle || "Advertiser")}</a>`}</strong><small>${escapeHtml(humanize(advertiser.platform || item.platform))} · ${escapeHtml(advertiser.external_id || item.advertiser_external_id || "—")}</small></td>
      <td class="source-name"><strong>${item.domain ? escapeHtml(item.domain) : "—"}</strong><small>${landing === "#" ? escapeHtml(item.landing_host || "No landing URL") : `<a href="${landing}" target="_blank" rel="noopener">Landing page ↗</a>`}</small></td>
      <td><span class="state-badge state-${escapeHtml(item.state || "unknown")}">${escapeHtml(humanize(item.state))}</span><small class="fit-label">${formatNumber(item.confidence || 0)} confidence</small></td>
      <td><div class="paid-match-features"><span class="${features.landing_domain ? "met" : ""}">Domain</span><span class="${features.social_handle ? "met" : ""}">Social</span><span class="${features.name ? "met" : ""}">Name</span></div>${evidenceLinks.length ? `<div class="link-list">${evidenceLinks.map((value, index) => `<a href="${safeUrl(typeof value === "string" ? value : value.url || value.source_url)}" target="_blank" rel="noopener">${index + 1}</a>`).join("")}</div>` : ""}</td>
      <td>${item.candidate_id ? `<span class="badge moss">#${formatNumber(item.candidate_id)}</span>` : `<span class="badge">Provisional</span>`}</td>
      <td><div class="suggestion-actions">${item.state !== "verified" ? `<button class="text-button" data-ad-link-action="verified" data-ad-link-id="${escapeHtml(item.id)}">Verify</button>` : ""}${item.state !== "rejected" ? `<button class="text-button muted" data-ad-link-action="rejected" data-ad-link-id="${escapeHtml(item.id)}">Reject</button>` : ""}</div></td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="paid-empty"><strong>No mappings in this state</strong><span>Advertiser Pages and landing domains appear after a paid scan imports results.</span></td></tr>`;
}

function renderAdDiscoveryOverview() {
  const overview = state.adDiscoveryOverview || {};
  const configured = adDiscoveryTokenConfigured(overview);
  const keyState = $("#paid-discovery-key-state");
  if (keyState) {
    keyState.textContent = configured ? "Apify configured" : "Apify token required";
    keyState.className = `badge ${configured ? "moss" : "warn"}`;
  }
  const inventory = overview.existing_target_inventory || {};
  const due = Number(inventory.due_candidates || 0);
  const total = Number(inventory.total_candidates || 0);
  const fresh = Number(inventory.fresh_candidates || 0);
  const deferred = Number(inventory.retry_deferred_candidates || 0);
  const active = Number(inventory.active_candidates || 0);
  const backlogState = $("#paid-discovery-backlog-state");
  if (backlogState) {
    backlogState.textContent = due ? `${formatNumber(due)} Meta due` : "Meta caught up";
    backlogState.className = `badge ${due ? "warn" : "moss"}`;
  }
  const backlogCopy = $("#paid-discovery-backlog-copy");
  if (backlogCopy) {
    backlogCopy.textContent = `${formatNumber(total)} relevant existing targets · ${formatNumber(due)} due now · ${formatNumber(fresh)} fresh for 90 days · ${formatNumber(deferred)} retry after 24 hours${active ? ` · ${formatNumber(active)} active` : ""}. Saving credentials prepares a Meta-only draft; Estimate still precedes any provider spend.`;
  }
  renderAdDiscoveryQuote();
  renderAdDiscoveryRun();
  renderAdDiscoveryLinks();
  renderDiscoveryTabs();
}

async function loadAdDiscoveryOverview({ includeStatus = true } = {}) {
  if (!$("#paid-discovery-form")) return;
  const requests = [api("/api/ad-discovery/overview"), api("/api/ad-discovery/links")];
  if (includeStatus) requests.push(api("/api/ad-discovery/status"));
  const [overviewResult, linksResult, statusResult] = await Promise.allSettled(requests);
  if (overviewResult.status === "fulfilled") state.adDiscoveryOverview = overviewResult.value;
  else showToast(overviewResult.reason.message, "error");
  if (linksResult.status === "fulfilled") {
    const payload = linksResult.value;
    state.adDiscoveryLinks = payload.items || payload.links || payload.results || (Array.isArray(payload) ? payload : []);
    state.adDiscoveryLinkTotal = Number(payload.total ?? state.adDiscoveryLinks.length);
  } else if (overviewResult.status === "fulfilled") {
    const fallback = overviewResult.value.links || [];
    state.adDiscoveryLinks = Array.isArray(fallback) ? fallback : (fallback.items || []);
    state.adDiscoveryLinkTotal = Number(fallback.total ?? state.adDiscoveryLinks.length);
  }
  if (includeStatus && statusResult?.status === "fulfilled") state.adDiscoveryRun = normalizeAdDiscoveryRun(statusResult.value);
  else if (includeStatus && overviewResult.status === "fulfilled" && overviewResult.value.status) {
    state.adDiscoveryRun = normalizeAdDiscoveryRun(typeof overviewResult.value.status === "object" ? overviewResult.value.status : { status: overviewResult.value.status });
  }
  renderAdDiscoveryOverview();
  if (overviewResult.status === "rejected") clearLoadingIndicators("#paid-link-body");
  if (adDiscoveryRunActive()) pollAdDiscoveryStatus();
}

async function pollAdDiscoveryStatus() {
  if (state.adDiscoveryPollInFlight || !$("#paid-discovery-form")) return;
  clearTimeout(state.adDiscoveryPollTimer);
  state.adDiscoveryPollInFlight = true;
  try {
    const payload = await api("/api/ad-discovery/status");
    const previous = state.adDiscoveryRun?.status;
    state.adDiscoveryRun = normalizeAdDiscoveryRun(payload);
    renderAdDiscoveryRun();
    if (adDiscoveryRunActive()) {
      state.adDiscoveryPollTimer = setTimeout(pollAdDiscoveryStatus, 2500);
    } else {
      if (previous && previous !== state.adDiscoveryRun?.status) showToast(`Paid scan ${state.adDiscoveryRun?.status || "finished"}`, scanToastTone(state.adDiscoveryRun?.status));
      await loadAdDiscoveryOverview({ includeStatus: false });
    }
  } catch {
    state.adDiscoveryPollTimer = setTimeout(pollAdDiscoveryStatus, 4000);
  } finally {
    state.adDiscoveryPollInFlight = false;
  }
}

async function updateAdDiscoveryLink(linkId, status, reviewNote = "") {
  try {
    const body = { state: status };
    if (reviewNote) body.review_note = reviewNote;
    await api(`/api/ad-discovery/links/${encodeURIComponent(linkId)}`, { method: "PATCH", body: JSON.stringify(body) });
    showToast(`Mapping ${status}`);
    await loadAdDiscoveryOverview({ includeStatus: false });
  } catch (error) { showToast(error.message, "error"); }
}

function humanizeScoutMessage(value = "") {
  if (value.includes("max_output_tokens")) return "Response limit reached";
  if (value.includes("TimeoutError")) return "Time limit reached";
  return value.replace(/^\w+Error:\s*/, "") || "No scans";
}

function elapsedLabel(startedAt) {
  const started = Date.parse(startedAt || "");
  if (!Number.isFinite(started)) return "Starting";
  const total = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds} elapsed`;
}

function updateScoutActivity() {
  const status = state.intelligence?.scout_status || {};
  if (status.status !== "running") return;
  $$('[data-scout-elapsed]').forEach(elapsed => { elapsed.textContent = elapsedLabel(status.started_at); });
  const age = state.scoutHeartbeatAt ? Math.max(0, Math.floor((Date.now() - state.scoutHeartbeatAt) / 1000)) : 0;
  $$('[data-scout-heartbeat]').forEach(heartbeat => { heartbeat.textContent = age < 2 ? "Server checked now" : `Server checked ${age}s ago`; });
}

function setScoutActivityTicker(running) {
  clearInterval(state.scoutActivityTimer);
  state.scoutActivityTimer = null;
  if (!running) return;
  updateScoutActivity();
  state.scoutActivityTimer = setInterval(updateScoutActivity, 1000);
}

function researchShell() {
  $("#view-research").innerHTML = `
    <div class="section-intro"><span class="section-meta" id="research-cycle">Loading…</span><button class="button" id="refresh-research">Refresh</button></div>
    <div class="cohort-contract" id="research-contract"></div>
    <div class="research-summary" id="research-summary"></div>
    <div class="research-layout">
      <section class="research-column"><div class="column-head"><h3>Missions</h3></div><div class="mission-list" id="mission-list"></div></section>
      <aside class="research-column"><div class="column-head"><h3>Source probes</h3></div><div class="probe-list" id="probe-list"></div></aside>
    </div>
    <section class="panel method-panel"><div class="panel-head"><h3>Target analysis</h3><span class="panel-meta" id="method-count"></span></div><div class="method-grid" id="method-grid"></div></section>
    <div class="research-attribution" id="research-attribution"></div>`;
  $("#refresh-research").addEventListener("click", loadResearch);
}

function renderResearch(data) {
  state.research = data;
  const focus = data.focus || {};
  const missions = Array.isArray(data.missions) ? data.missions : [];
  const probes = Array.isArray(data.source_probes) ? data.source_probes : [];
  const methods = Array.isArray(data.analysis_methods) ? data.analysis_methods : [];
  const publicSources = Array.isArray(data.public_sources) ? data.public_sources : [];
  const pool = data.strategy_pool || {};
  const contract = data.operating_contract || {};
  $("#research-cycle").textContent = `Cycle ${data.cycle} · ${new Date(data.generated_at).toLocaleDateString()}`;
  $("#research-contract").innerHTML = `<strong>Primary cohort</strong><span>${escapeHtml(contract.primary_cohort || "Paid-growth consumer and digital buyers")}</span><small>${escapeHtml(contract.referral_cohort || "Referral partners")} · ${escapeHtml(roundDollarText(contract.offer || "$5,000/month"))} · ${escapeHtml(contract.automation_threshold || "20 scan calls and 3 renewal decisions")}</small>`;
  $("#research-summary").innerHTML = `<div><small>Focus</small><strong>${escapeHtml(focus.segment || "—")}</strong></div><div><small>Strategy variants</small><strong>${formatNumber(pool.combinations)}</strong></div><div><small>Source lenses</small><strong>${formatNumber(pool.source_lenses)}</strong></div><div><small>Daily missions</small><strong>${formatNumber(missions.length)}</strong></div><div><small>Free methods</small><strong>${formatNumber(methods.length)}</strong></div><div><small>Source probes</small><strong>${formatNumber(probes.length)}</strong></div>`;
  $("#mission-list").innerHTML = missions.length ? missions.map((mission, index) => `<article class="mission-card">
    <header><span>${String(index + 1).padStart(2, "0")}</span><div><h4>${escapeHtml(mission.name)}</h4><small>${escapeHtml(mission.segment)} · ${escapeHtml(mission.region || "Global")}</small><small class="mission-variant">${escapeHtml(humanize(mission.relationship_type || "buyer"))} · ${escapeHtml(mission.proof_mode || "Public proof")} · ${escapeHtml(mission.recency || "Current")}</small></div></header>
    <code>${escapeHtml(mission.query)}</code>
    <dl><div><dt>Evidence</dt><dd>${escapeHtml(mission.evidence)}</dd></div><div><dt>Next</dt><dd>${escapeHtml(mission.next_step)}</dd></div></dl>
    <footer><button class="text-button" data-copy-query="${escapeHtml(mission.query)}">Copy query</button>${(mission.urls || []).map(item => `<a href="${safeUrl(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.engine)} ↗</a>`).join("")}</footer>
  </article>`).join("") : `<p class="empty">No missions.</p>`;
  $("#probe-list").innerHTML = probes.length ? probes.map(probe => `<article class="probe-card">
    <div class="probe-head"><div><strong>${escapeHtml(probe.apex)}</strong><small>${escapeHtml(probe.category)} · ${formatNumber(probe.candidate_count)} sampled</small></div><span>${Number(probe.opportunity_score || 0)}</span></div>
    <div class="probe-stat"><span>Uninspected</span><strong>${formatNumber(probe.uninspected)}</strong></div>
    <div class="probe-links">${(probe.links || []).map(link => `<a href="${safeUrl(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.name)} ↗</a>`).join("")}</div>
  </article>`).join("") : `<p class="empty">No sources.</p>`;
  $("#method-count").textContent = `${formatNumber(methods.length)} methods`;
  $("#method-grid").innerHTML = methods.map((method, index) => `<div class="method-row"><span>${String(index + 1).padStart(2, "0")}</span><strong>${escapeHtml(method.name)}</strong><p>${escapeHtml(method.signal)}</p><small>${escapeHtml(method.cost)}</small></div>`).join("");
  $("#research-attribution").innerHTML = publicSources.map(source => `<a href="${safeUrl(source.url)}" target="_blank" rel="noopener">${escapeHtml(source.name)} · ${escapeHtml(source.terms)} ↗</a>`).join("");
}

async function loadResearch() {
  if (!$("#mission-list")) return;
  $("#mission-list").innerHTML = `<p class="empty">${loadingIndicator()}</p>`;
  try { renderResearch(await api("/api/research")); }
  catch (error) { showToast(error.message, "error"); $("#mission-list").innerHTML = `<p class="empty">Research unavailable.</p>`; }
}

function renderIntelligence(data) {
  state.intelligence = data;
  const suggestions = Array.isArray(data.suggestions) ? data.suggestions : [];
  const pendingSuggestions = suggestions.filter(item => item.status === "pending");
  const counts = data.counts || {};
  const liveStatus = data.scout_status || {};
  const latest = data.latest_scout || {};
  const status = liveStatus.status && liveStatus.status !== "idle" ? liveStatus.status : (latest.status || "idle");
  const summary = latest.summary || {};
  const rawMessage = liveStatus.status && liveStatus.status !== "idle" ? liveStatus.message : (summary.error || liveStatus.message || "");
  const terminalScoutStatus = ["complete", "partial"].includes(status);
  const returnedCount = Number(summary.suggestions ?? liveStatus.suggestions ?? suggestions.length);
  const detail = terminalScoutStatus
    ? `${formatNumber(returnedCount)} source${returnedCount === 1 ? "" : "s"} returned`
    : humanizeScoutMessage(rawMessage);
  $$('[data-scout-state]').forEach(root => { root.textContent = humanize(status); });
  const messageMarkup = status === "running" ? `
    <div class="scout-activity-copy">
      <div class="scout-activity-title"><i aria-hidden="true"></i><strong>Public source research active</strong></div>
    </div>
    <div class="scout-activity-meta">
      <strong data-scout-elapsed>${escapeHtml(elapsedLabel(liveStatus.started_at))}</strong>
      <small data-scout-heartbeat>Server checked now</small>
    </div>
    <div class="scout-activity-track" role="progressbar" aria-label="Source scout in progress" aria-valuetext="Searching and validating public sources"><span></span></div>`
    : `<div><strong>${status === "idle" ? "Idle" : escapeHtml(humanize(status))}</strong>${status === "idle" ? "" : `<span title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>`}</div>`;
  $$('[data-scout-message]').forEach(root => {
    root.className = `scout-message ${status}`;
    root.innerHTML = messageMarkup;
    root.hidden = !["running", "failed"].includes(status);
  });
  setScoutActivityTicker(status === "running");
  const statsMarkup = `
    <span><strong>${formatNumber(counts.pending)}</strong> pending</span>
    <span><strong>${formatNumber(counts.accepted)}</strong> accepted</span>
    <span><strong>${formatNumber(returnedCount)}</strong> returned</span>`;
  $$('[data-scout-stats]').forEach(root => { root.innerHTML = statsMarkup; });
  const emptyState = status === "running"
    ? "Pending"
    : status === "failed"
      ? detail
      : terminalScoutStatus
        ? "No new sources"
        : "No scans";
  const suggestionMarkup = pendingSuggestions.length ? pendingSuggestions.map(item => {
    const relationshipLabel = item.relationship_type === "referral_partner" ? "Referral" : "Buyer";
    const familyLabel = humanize(item.source_family || "other");
    const archetypeLabel = (item.buyer_archetypes || []).slice(0, 3).map(humanize).join(" · ");
    return `<tr>
      <td class="source-name"><strong><a href="${safeUrl(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.name)}</a></strong><small>${escapeHtml([item.apex || item.source_key, relationshipLabel].filter(Boolean).join(" · "))}</small></td>
      <td><span class="badge">${escapeHtml(String(item.source_kind || "unknown").replaceAll("_", " "))}</span><small>${escapeHtml([familyLabel, archetypeLabel].filter(Boolean).join(" · "))}</small></td>
      <td><span class="score">${Number(item.intelligence_score || 0)}</span><small class="fit-label">${escapeHtml(String(item.confidence || "unknown").replaceAll("_", " "))}</small></td>
      <td class="mechanism">${escapeHtml(item.discovery_mechanism)}</td>
      <td><div class="link-list">${(item.evidence_urls || []).slice(0, 3).map((url, index) => `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${index + 1}</a>`).join("") || "—"}</div></td>
      <td><span class="badge">${escapeHtml(item.status)}</span></td>
      <td><div class="suggestion-actions"><button class="text-button" data-suggestion-action="accepted" data-suggestion-id="${item.id}">Accept</button><button class="text-button muted" data-suggestion-action="rejected" data-suggestion-id="${item.id}">Reject</button></div></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="scout-empty ${status === "running" ? "is-running" : ""}">${status === "running" ? `<div class="scout-working-lines" aria-hidden="true"><i></i><i></i><i></i></div>` : ""}<strong>${escapeHtml(status === "failed" ? "Scan failed" : status === "running" ? "Collecting sources" : suggestions.length ? "No source ideas awaiting review" : emptyState)}</strong>${status === "failed" ? `<span>${escapeHtml(emptyState)}</span>` : ""}</td></tr>`;
  $$('[data-suggestion-body]').forEach(root => { root.innerHTML = suggestionMarkup; });
  syncSourceScoutControls();
  renderDiscoveryTabs();
}

function applySourceReview(payload, suggestion) {
  if(!suggestion || !state.intelligence)return false;
  sourceReviewGeneration++;
  const id=String(payload.id), requested=String(payload.requested_id);
  const matches=item=>[id,requested].includes(String(item.id));
  const oldStatus=(state.intelligence.suggestions || []).find(matches)?.status || suggestion.status || "pending";
  const counts={...(state.intelligence.counts || {})};
  if(oldStatus !== payload.status) {
    if(Number.isFinite(counts[oldStatus]))counts[oldStatus]=Math.max(0,counts[oldStatus]-1);
    if(Number.isFinite(counts[payload.status]))counts[payload.status]++;
  }
  const intelligence={...state.intelligence,counts,
    suggestions:(state.intelligence.suggestions || []).map(item=>matches(item)?
      {...item,status:payload.status}:item)};
  renderIntelligence(intelligence);
  const source=payload.source;
  state.acceptedSources=(state.acceptedSources || []).filter(item=>!matches(item));
  state.discoveryItems=(state.discoveryItems || []).filter(item=>!matches(item));
  if(payload.status === "accepted" && source) {
    state.acceptedSources.push(source);
    if(source.queue_visible)state.discoveryItems.push(source);
  }
  if($("#source-groups"))renderSources();
  // The next navigation reloads exact catalogues; no stale persisted snapshot may restore a review.
  try {localStorage.removeItem(SOURCES_VIEW_CACHE_KEY);} catch {}
  state.initializedViews.delete("intelligence");
  state.discoveryEstimates={};
  renderAggregateScanState();
  return true;
}

async function loadIntelligence({ useCached = false } = {}) {
  const generation=sourceReviewGeneration;
  if (!$('[data-suggestion-body]')) return;
  const cached = useCached ? readSourcesViewCache()?.intelligence : null;
  if (cached && typeof cached === "object") renderIntelligence(cached);
  try {
    const data = await api("/api/intelligence");
    if(generation !== sourceReviewGeneration)return;
    renderIntelligence(data);
    writeSourcesViewCache("intelligence", data);
    if (data.scout_status?.status === "running") pollSourceScout();
  }
  catch (error) { showToast(error.message, "error"); clearLoadingIndicators('[data-suggestion-body]'); }
}

async function pollSourceScout() {
  if (state.scoutPollInFlight) return;
  clearTimeout(state.scoutPollTimer);
  state.scoutPollInFlight = true;
  try {
    const status = await api("/api/source-scout-status");
    state.scoutHeartbeatAt = Date.now();
    if (state.intelligence) renderIntelligence({ ...state.intelligence, scout_status: status });
    if (status.status === "running") {
      updateScoutActivity();
      state.scoutPollTimer = setTimeout(pollSourceScout, 2500);
    }
    if (["complete", "partial"].includes(status.status)) { showToast(status.message || `${formatNumber(status.suggestions)} sources found`, scanToastTone(status.status)); await loadIntelligence(); }
    if (status.status === "failed") { showToast(humanizeScoutMessage(status.message || "Source scout failed"), "error"); await loadIntelligence(); }
  } catch {
    state.scoutPollTimer = setTimeout(pollSourceScout, 4000);
  } finally {
    state.scoutPollInFlight = false;
  }
}

function renderRuns() {
  $("#run-list").innerHTML = state.runs.length ? state.runs.map(run => {
    const summary = run.summary || {};
    const retryableHosts = summary.retryable_hosts || [];
    const inspectionFailures = Number(summary.inspection_errors?.count || 0);
    const fallbackFailures = inspectionFailures + Number(summary.source_errors || 0) + (summary.ai_errors?.length || 0) + (run.status === "failed" ? 1 : 0);
    const failed = Number(summary.failure_count ?? fallbackFailures);
    const succeeded = Number(summary.success_count ?? (run.status === "failed" ? 0 : Math.max(Number(summary.candidates_discovered || 0) - inspectionFailures, 0)));
    const failureItems = [
      ...(summary.inspection_errors?.samples || []),
      ...(summary.free_intelligence?.errors || []),
      ...(summary.news_intelligence?.errors || []),
      ...(summary.common_crawl?.errors || []),
      ...(summary.ai_errors || []),
    ];
    if (summary.source_errors) failureItems.unshift(`${summary.source_errors} source scan failure${summary.source_errors === 1 ? "" : "s"}`);
    if (summary.error) failureItems.unshift(summary.error);
    const report = failureItems.length ? `<details class="run-report"><summary>Failures · ${failureItems.length}</summary><ul>${failureItems.slice(0, 12).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : "";
    const resume = ["failed", "partial"].includes(run.status) && retryableHosts.length ? `<div class="run-actions"><button class="text-button" data-resume-run="${run.id}">Retry ${formatNumber(retryableHosts.length)} failed</button></div>` : "";
    return `<article class="run-row"><div><small>Scan</small><strong>#${run.id}</strong></div><div><small>Status</small><strong class="run-status">${escapeHtml(run.status)}</strong></div><div><small>Finished</small><strong>${absoluteDate(run.finished_at || run.started_at)}</strong></div><div><small>Sources</small><strong>${formatNumber(summary.sources_requested)}</strong></div><div><small>Discovered</small><strong>${formatNumber(summary.candidates_discovered)}</strong></div><div><small>Live</small><strong>${formatNumber(summary.sites_live)}</strong></div><div><small>Changed</small><strong>${formatNumber(summary.materially_changed)}</strong></div><div class="run-outcome"><span class="run-good">${formatNumber(succeeded)} succeeded</span><span class="run-bad">${formatNumber(failed)} failed</span></div>${resume}${report}</article>`;
  }).join("") : `<div class="panel"><div class="empty-state"><strong>No scans</strong></div></div>`;
}

async function loadRuns() {
  try { const result = await api("/api/runs"); state.runs = result.items; if ($("#run-list")) renderRuns(); }
  catch (error) { showToast(error.message, "error"); clearLoadingIndicators("#run-list"); }
}

async function openProspect(candidateId) {
  closeAccountWorkspace();
  const drawer = $("#prospect-drawer"), backdrop = $("#drawer-backdrop");
  $("#drawer-content").innerHTML = `<div class="drawer-loading">${loadingIndicator("Loading target…")}</div>`;
  drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); backdrop.hidden = false;
  try {
    const [detail, similarResult] = await Promise.all([
      api(`/api/prospects/${candidateId}`),
      api(`/api/prospects/${candidateId}/similar?limit=10`).catch(() => ({ items: [] })),
    ]);
    const listed = state.prospects.find(value => value.candidate_id === Number(candidateId)) || {};
    const item = {
      ...listed,
      ...detail,
      score: { ...(listed.score || {}), ...(detail.score || {}) },
      target_intelligence: detail.target_intelligence || listed.target_intelligence,
    };
    const intelligence = targetIntel(item);
    const capacity = intelligence.hypothesis;
    const siteUrl = safeUrl(item.company_url);
    const companyTitle = siteUrl === "#"
      ? escapeHtml(companyName(item))
      : `<a href="${siteUrl}" target="_blank" rel="noopener noreferrer">${escapeHtml(companyName(item))}</a>`;
    const claimRows = (claims, epistemic) => (claims || []).slice(0, 5).map(claim => {
      const value = typeof claim === "string" ? { claim } : claim;
      return `<li><p>${escapeHtml(compactEvidenceCopy(value.claim || value.summary || value.label || "—"))}</p><div class="claim-meta"><span>${escapeHtml(humanize(value.epistemic_status || epistemic))}</span><span>${escapeHtml(value.observed_at ? absoluteDate(value.observed_at) : "—")}</span><span>${escapeHtml(humanize(value.confidence || "unknown"))} confidence</span>${value.source_url ? `<a href="${safeUrl(value.source_url)}" target="_blank" rel="noopener">Source ↗</a>` : ""}</div></li>`;
    }).join("");
    const capacityObservations = claimRows(capacity.observations || capacity.observed_claims || item.target_intelligence?.observations, "observed");
    const gaps = [...intelligence.evidence_gaps, ...intelligence.unassessable_criteria, ...intelligence.blockers]
      .filter(Boolean)
      .filter((value, index, values) => values.findIndex(candidate => JSON.stringify(candidate) === JSON.stringify(value)) === index)
      .slice(0, 6);
    const gapItems = gaps.map(value => `<li>${escapeHtml(typeof value === "string" ? value : value.label || value.criterion || value.message || JSON.stringify(value))}</li>`).join("");
    const similar = (similarResult.items || []).slice(0, 6).map(peer => `<button class="similar-target" data-prospect-id="${peer.candidate_id}"><span><strong>${escapeHtml(companyName(peer))}</strong><small>${escapeHtml(targetIntel(peer).lane === "referral_partner" ? "Agency partner" : humanize(targetIntel(peer).scope))}</small></span><b>${targetIntel(peer).priority_score}</b></button>`).join("");
    const fitCopy = intelligence.hydration_status === "hydrated"
      ? `${intelligence.principal_signals_met} of 3 core buying signals are supported by public evidence. Recommended action: ${humanize(intelligence.recommended_action).toLowerCase()} for this ${intelligence.lane === "referral_partner" ? "agency relationship" : "direct buyer"}.`
      : "This company still needs enough public evidence for a reliable fit assessment.";
    const scanAttempts = Number(item.scan_attempt_count || 0);
    const scanFailures = Number(item.scan_failure_count || 0);
    const scanBreakdown = (item.scan_failure_by_type || []).map(scan => `<li><span>${escapeHtml(humanize(String(scan.scanner_id || "scanner").replace(/_v\d+$/, "")))}</span><strong>${Number(scan.failure_rate || 0).toFixed(1)}%</strong><small>${formatNumber(scan.failure_count)} / ${formatNumber(scan.attempt_count)} failed</small></li>`).join("");
    $("#drawer-content").innerHTML = `
      <header class="drawer-head intelligence-drawer-head"><div>${item.source_apex ? `<p class="eyebrow">${escapeHtml(item.source_apex)}</p>` : ""}<h2>${companyTitle}</h2><p>${escapeHtml(item.canonical_domain || item.canonical_host || item.hostname || "")}</p></div><div class="drawer-head-actions"><button class="button" id="scan-company" type="button">Scan company</button><button class="icon-button" id="close-drawer" aria-label="Close dossier">×</button></div></header>
      <div class="drawer-body target-dossier">
        ${contactActions(item, { detailed: true })}
        <section class="target-dossier-hero"><div class="target-dossier-scores"><div><small>Fit</small><strong>${intelligence.hydration_status === "hydrated" ? intelligence.durable_fit_score : "—"}</strong></div><div><small>Now</small><strong>${intelligence.hydration_status === "hydrated" ? intelligence.contact_now_score : "—"}</strong></div><div><small>Priority</small><strong>${intelligence.hydration_status === "hydrated" ? intelligence.founder_priority_score : "—"}</strong></div></div><div class="target-dossier-meta"><span>${escapeHtml(humanize(intelligence.recommended_action))}</span><span>${escapeHtml(intelligence.lane === "referral_partner" ? "Agency partner" : humanize(intelligence.scope))}</span><small>${escapeHtml(targetFreshness(item))}</small></div></section>
        ${providerPeoplePanel(item)}
        <section class="drawer-section scan-reliability-panel"><div class="section-title-row"><div><h3>Scan reliability</h3><p>${scanAttempts ? `${Number(item.scan_failure_rate || 0).toFixed(1)}% failure rate · ${formatNumber(scanFailures)} of ${formatNumber(scanAttempts)} attempts` : "No terminal scanner history yet"}</p></div><label class="scan-policy-toggle"><input id="company-scan-enabled" type="checkbox" ${item.scan_enabled !== false ? "checked" : ""} /><span>Include in scans</span></label></div>${scanBreakdown ? `<ul>${scanBreakdown}</ul>` : ""}</section>
        <section class="drawer-section fit-summary"><h3>Why this target</h3><p>${escapeHtml(fitCopy)}</p>${targetSignals(item, { detailed: true })}</section>
        <section class="drawer-section why-now-panel"><div><h3>Why now</h3><p class="trigger-lead">${escapeHtml(targetWhyNow(item))}</p></div></section>
        ${paidAdEvidencePanel(item)}
        ${seoSignalPanel(item)}
        <section class="drawer-section"><div class="section-title-row"><h3>Best evidence</h3><span class="evidence-status evidence-observed">Observed</span></div><ul class="target-claim-list">${capacityObservations || `<li><p>No strong first-party claims yet.</p></li>`}</ul></section>
        <section class="drawer-section evidence-gaps"><h3>What to verify</h3><ul>${gapItems || `<li>No material evidence gaps are recorded.</li>`}</ul></section>
        <section class="drawer-section"><div class="section-title-row"><h3>Similar targets</h3><span class="target-peer-count">${formatNumber(similarResult.total || 0)}</span></div><div class="similar-targets">${similar || `<p class="form-note">None.</p>`}</div></section>
        <footer class="dossier-footer"><button class="exclude-company-button ${item.excluded ? "restore" : ""}" id="detail-exclude">${item.excluded ? "Restore company" : "Exclude from targets"}</button><span>Use exclusion only to improve future ranking quality.</span></footer>
      </div>`;
    $("#close-drawer").addEventListener("click", closeDrawer);
    $("#scan-company").addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = "Starting…";
      try {
        const job = await api(`/api/prospects/${candidateId}/scan`, { method: "POST" });
        state.webScanJobId = job.job_id || null;
        state.scanStatus = job.status || "running";
        button.textContent = "Scan started";
        showToast(`Scanning ${companyName(item)} now`);
        pollScanStatus();
      } catch (error) {
        button.disabled = false;
        button.textContent = "Scan company";
        showToast(error.message, "error");
      }
    });
    $("#detail-exclude").addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      const excluded = !item.excluded;
      try {
        await api(`/api/prospects/${candidateId}/workflow`, { method: "PATCH", body: JSON.stringify({ excluded }) });
        showToast(excluded ? "Company excluded" : "Company restored");
        closeDrawer();
        await refreshVisibleData();
      } catch (error) {
        button.disabled = false;
        showToast(error.message, "error");
      }
    });
    $("#company-scan-enabled").addEventListener("change", async event => {
      const toggle = event.currentTarget;
      toggle.disabled = true;
      try {
        const workflow = await api(`/api/prospects/${candidateId}/workflow`, {
          method: "PATCH",
          body: JSON.stringify({ scan_enabled: toggle.checked }),
        });
        item.scan_enabled = workflow.scan_enabled;
        const listedItem = state.prospects.find(value => value.candidate_id === Number(candidateId));
        if (listedItem) listedItem.scan_enabled = workflow.scan_enabled;
        showToast(workflow.scan_enabled ? "Company included in scans" : "Company excluded from scans");
      } catch (error) {
        toggle.checked = !toggle.checked;
        showToast(error.message, "error");
      } finally {
        toggle.disabled = false;
      }
    });
  } catch (error) { $("#drawer-content").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; }
}

function closeDrawer() {
  $("#prospect-drawer").classList.remove("open"); $("#prospect-drawer").setAttribute("aria-hidden", "true"); $("#drawer-backdrop").hidden = true;
}

function syncGoButton(status = state.scanStatus) {
  const active = ["running", "retrying"].includes(status);
  const webButton = $("#run-web-scan");
  if (webButton) {
    webButton.disabled = state.webScanStarting || active || scanControlLocks.has($("#web-scan-form"));
    webButton.textContent = state.webScanStarting ? "Starting…" : active ? "Scan scanning" : `Scan web · ${scanCapLabel(state.webScanEstimate)}${state.webScanEstimate?.provider_cost_usd == null ? "" : " max"}`;
  }
}

async function pollScanStatus() {
  if (state.scanPollInFlight) return;
  clearTimeout(state.scanPollTimer);
  state.scanPollInFlight = true;
  try {
    const status = await api(`/api/scan-status${state.webScanJobId ? `?job_id=${encodeURIComponent(state.webScanJobId)}` : ""}`);
    const previous = state.scanStatus;
    state.scanStatus = status.status;
    state.webScanStatus = status;
    const shell = $("#smart-scan-shell");
    if (shell) {
      delete shell.dataset.overviewPending;
      if (shell.dataset.initialLoading !== "true") delete shell.dataset.scanStatusUnavailable;
    }
    renderImportScanProgress();
    state.scanOverview = status.scan_overview || null;
    if (state.smartScan && $("#smart-scan-shell")?.dataset.initialLoading !== "true") {
      state.smartScan.external_source_runs = status.scan_overview?.methods || [];
      renderSmartScan(state.smartScan);
    }
    const active = ["running", "retrying"].includes(status.status);
    const wasActive = ["running", "retrying"].includes(previous);
    syncGoButton(status.status);
    renderAggregateScanState();
    state.scanPollTimer = setTimeout(pollScanStatus, active ? 2500 : 5000);
    if (["complete", "partial"].includes(status.status) && wasActive) {
      const terminalMessage = status.status === "partial" ? "Scan completed with partial results" : "Scan complete";
      showToast(status.message || terminalMessage, scanToastTone(status.status));
      invalidateTargetOverviewCache();
      await refreshVisibleData();
    }
    if (status.status === "failed" && previous !== "failed") showToast(status.message || "Scan failed", "error");
  } catch {
    const shell = $("#smart-scan-shell");
    if (shell?.dataset.overviewPending === "true") {
      shell.dataset.scanStatusUnavailable = "true";
      renderScanLauncherStatus();
    }
    state.scanPollTimer = setTimeout(pollScanStatus, 4000);
  } finally {
    state.scanPollInFlight = false;
  }
}

let outboundGeneration = 0;
let outboundOffset = 0;
let outboundStatus = "";
let outboundData = null;
const outboundPageSize = 50;

async function openOutboundNote(row) {
  const dialog = document.createElement("dialog");
  dialog.className = "modal compact outbound-note-dialog";
  dialog.setAttribute("aria-labelledby", "outbound-note-title");
  dialog.innerHTML = `<form class="modal-card">
    <header class="modal-head"><h2 id="outbound-note-title" tabindex="-1" autofocus>Note · ${escapeHtml(row.name)}</h2>
      <button type="button" class="icon-button" data-note-close aria-label="Close note">×</button></header>
    <div class="modal-body"><label class="field"><span>Outreach text or notes</span>
      <textarea aria-label="Contact note" maxlength="100000" rows="12" placeholder="Paste your outreach text here…" disabled></textarea></label>
      <span class="sr-only" data-note-message role="status">Loading…</span></div>
    <footer class="modal-actions"><button type="button" class="button" data-note-copy disabled>Copy</button>
      <button type="button" class="button" data-note-close>Cancel</button>
      <button type="submit" class="button primary" data-note-save disabled>Save</button></footer></form>`;
  const area = dialog.querySelector("textarea");
  const message = dialog.querySelector("[data-note-message]");
  const save = dialog.querySelector("[data-note-save]");
  const copy = dialog.querySelector("[data-note-copy]");
  let original = "", busy = false;
  const close = () => {
    if (busy) return;
    if (area.value !== original && !window.confirm("Discard unsaved note changes?")) return;
    dialog.close();
  };
  dialog.querySelectorAll("[data-note-close]").forEach(button => button.addEventListener("click", close));
  dialog.addEventListener("cancel", event => {event.preventDefault(); close();});
  dialog.addEventListener("close", () => dialog.remove(), {once:true});
  copy.addEventListener("click", async () => {
    try {await copyText(area.value); message.textContent = "Copied";}
    catch {message.textContent = "Could not copy. Select the text and copy it manually."; showToast(message.textContent, "error"); area.focus(); area.select();}
  });
  dialog.querySelector("form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || area.disabled) return;
    busy = true; save.disabled = true; area.readOnly = true;
    save.textContent = "Saving…";
    message.textContent = "Saving…";
    try {
      await api(`/api/sales-engine/outbound/${row.id}/note`, {method:"PUT", body:JSON.stringify({note:area.value})});
      original = area.value; dialog.close();
      if (state.activeView === "prospects") await loadOutboundProspects();
      showToast("Note saved");
    } catch (error) {message.textContent = error.message; showToast(error.message, "error");}
    finally {busy = false; save.disabled = false; save.textContent = "Save"; area.readOnly = false;}
  });
  document.body.append(dialog); dialog.showModal();
  try {
    const data = await api(`/api/sales-engine/outbound/${row.id}/note`);
    if (!dialog.isConnected) return;
    original = data.note; area.value = original;
    area.disabled = false; save.disabled = false; copy.disabled = false;
    message.textContent = ""; area.focus();
  } catch (error) {if (dialog.isConnected) {message.textContent = error.message; showToast(error.message, "error");}}
}

async function editOutboundProspect(id) {
  try {
    await setView("prospects");
    const prospect = await api(`/api/sales-engine/outbound/${id}`);
    openManualProspect(prospect, prospect, async () => {
      invalidateSalesWorkbench();
      if (state.activeView === "prospects") await loadOutboundProspects();
    });
  } catch (error) {showToast(error.message, "error");}
}

function outboundShell() {
  $("#view-prospects").innerHTML = `<section class="panel outbound-panel page-body">
    <div class="panel-head outbound-header"><h3 id="outbound-title">Saved people <span id="outbound-total"></span></h3>
      <div class="outbound-actions">
        <select id="outbound-filter" aria-label="Filter prospects by status"><option value="">All statuses</option></select>
        <a class="button" id="outbound-export" href="/api/sales-engine/outbound/export.csv" title="Export all saved prospects">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/></svg>Export CSV
        </a>
      </div></div>
    <div id="outbound-message" role="status"></div>
    <div class="table-wrap outbound-scroll" role="region" aria-labelledby="outbound-title" tabindex="0"><table class="outbound-table">
      <thead><tr><th scope="col">Person</th><th scope="col">Company</th><th scope="col">Contact</th><th scope="col">Phone</th><th scope="col">Status</th><th scope="col">Updated</th></tr></thead>
      <tbody id="outbound-rows"></tbody></table></div>
    <div class="outbound-paging"><span id="outbound-range"></span><button class="button" id="outbound-prev">Previous</button>
      <button class="button" id="outbound-next">Next</button></div></section>`;
  $("#outbound-filter").addEventListener("change", event => {
    outboundStatus = event.target.value; outboundOffset = 0; void loadOutboundProspects();
  });
  $("#outbound-prev").addEventListener("click", () => {outboundOffset = Math.max(0,outboundOffset-outboundPageSize); void loadOutboundProspects();});
  $("#outbound-next").addEventListener("click", () => {outboundOffset += outboundPageSize; void loadOutboundProspects();});
  $("#outbound-rows").addEventListener("click", event => {
    const lookup = event.target.closest("[data-outbound-lookup] button");
    if (lookup) {
      const id = Number(lookup.closest("[data-outbound-lookup]").dataset.outboundLookup);
      const row = outboundData?.items.find(item => item.id === id);
      if (row && !lookup.disabled) void runContactLookup(lookup.dataset.engine, row, {
        ...row, prospect_key:row.person_key,
      });
      return;
    }
    const edit = event.target.closest("[data-outbound-edit]");
    if (edit) {void editOutboundProspect(Number(edit.dataset.outboundEdit)); return;}
    const note = event.target.closest("[data-outbound-note]");
    if (note) {
      const row = outboundData?.items.find(item => item.id === Number(note.dataset.outboundNote));
      if (row) void openOutboundNote(row);
      return;
    }
    const company = event.target.closest("[data-outbound-company]");
    if (company) {event.preventDefault(); void openSalesCompany(Number(company.dataset.outboundCompany), company.dataset.companyKey).catch(error=>showToast(error.message, "error"));}
  });
  $("#outbound-rows").addEventListener("change", async event => {
    const control = event.target.closest("[data-outbound-status], [data-outbound-method], [data-outbound-touchpoint]");
    if (!control) return;
    const isTouchpoint = control.dataset.outboundTouchpoint !== undefined;
    const isMethod = isTouchpoint || control.dataset.outboundMethod !== undefined;
    if (isTouchpoint && !control.value) return;
    const row = outboundData.items.find(item => item.id === Number(isTouchpoint ? control.dataset.outboundTouchpoint : isMethod ? control.dataset.outboundMethod : control.dataset.outboundStatus));
    if (!row) return;
    const previous = isTouchpoint ? "" : isMethod ? row.contact_method : row.status;
    control.disabled = true;
    try {
      await api(`/api/sales-engine/outbound/${row.id}${isTouchpoint ? "/touchpoints" : isMethod ? "/contact-method" : ""}`, {
        method:isTouchpoint ? "POST" : isMethod ? "PUT" : "PATCH",
        body:JSON.stringify(isMethod ? {contact_method:control.value} : {status:control.value}),
      });
      if (state.activeView === "prospects") await loadOutboundProspects();
    } catch (error) {control.value = previous; showToast(error.message, "error");}
    finally {control.disabled = false;}
  });
}
function outboundPhoneLookup(row) {
  return contactLookupActions({...row, prospect_key:row.person_key}, {
    ...row, email_lookup:state.scanReadiness?.treg,
  }).phone || '<span class="muted">—</span>';
}
function syncOutboundLookupActions() {
  if (state.activeView !== "prospects") return;
  for (const control of $$("[data-outbound-lookup]")) {
    const row = outboundData?.items.find(item => item.id === Number(control.dataset.outboundLookup));
    if (!row) continue;
    control.innerHTML = outboundPhoneLookup(row);
  }
}
async function loadOutboundProspects() {
  const generation = ++outboundGeneration;
  document.dispatchEvent(new CustomEvent("beings-page-status", {detail:{text:"Loading…"}}));
  try {
    const data = await api(`/api/sales-engine/outbound?status=${encodeURIComponent(outboundStatus)}&search=${encodeURIComponent(state.pageSearch)}&offset=${outboundOffset}&limit=${outboundPageSize}`);
    if (generation !== outboundGeneration || state.activeView !== "prospects") return;
    if (data.total && outboundOffset >= data.total) {outboundOffset = Math.floor((data.total-1)/outboundPageSize)*outboundPageSize; return loadOutboundProspects();}
    outboundData = data;
    const methodOptions = selected => Object.entries(data.contact_methods || {"":"Not set",email:"Email",linkedin:"LinkedIn",phone:"Phone",other:"Other"}).map(([key,label]) => `<option value="${key}" ${selected === key ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
    const options = selected => Object.entries(data.statuses).map(([key,label])=>`<option value="${key}" ${selected === key ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
    $("#outbound-filter").innerHTML = `<option value="">All statuses (${Object.values(data.counts).reduce((a,b)=>a+b,0)})</option>` + Object.entries(data.statuses).map(([key,label])=>`<option value="${key}">${escapeHtml(label)} (${data.counts[key] || 0})</option>`).join("");
    $("#outbound-filter").value = outboundStatus;
    $("#outbound-total").textContent = formatNumber(data.total);
    $("#outbound-message").textContent = "";
    $("#outbound-rows").innerHTML = data.items.map(row => {
      return `<tr class="outbound-row">
      <td><div class="outbound-person">
        <div class="outbound-identity"><strong>${escapeHtml(row.name)}</strong>${row.title ? `<small>${escapeHtml(row.title)}</small>` : ""}</div>
        <div class="outbound-person-actions">
          <button type="button" class="text-button" data-outbound-edit="${row.id}" aria-label="Edit ${escapeHtml(row.name)}">Edit</button>
          <button type="button" class="icon-button outbound-note ${row.has_note ? "has-note" : ""}" data-outbound-note="${row.id}" title="${row.has_note ? "Edit note" : "Add note"}" aria-label="${row.has_note ? "Edit" : "Add"} note for ${escapeHtml(row.name)}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h6"/></svg>
          </button>
        </div>
      </div></td>
      <td class="outbound-company"><a href="#sales" data-outbound-company="${row.candidate_id}" data-company-key="${escapeHtml(row.company_key)}">${escapeHtml(String(row.company_name ?? "").replaceAll("&amp;", "&"))}</a><small>${escapeHtml(row.company_key)}</small></td>
      <td><div class="outbound-contact">
        ${row.email ? `<div class="outbound-contact-line outbound-email-line"><button type="button" class="text-button" data-copy-email="${escapeHtml(row.email)}" aria-label="Copy ${escapeHtml(row.email)}">${escapeHtml(row.email)}</button></div>` : ""}
        ${/^https?:\/\//i.test(row.profile_url || "") ? `<div class="outbound-contact-line"><a href="${escapeHtml(row.profile_url)}" target="_blank" rel="noopener noreferrer">Profile ↗</a></div>` : ""}
        ${!row.email && !row.profile_url ? '<span class="muted">—</span>' : ""}
      </div></td>
      <td class="outbound-phone">${safeTel(row.phone) ? `<a href="${escapeHtml(safeTel(row.phone))}">${escapeHtml(formatPhoneNumber(row.phone))}</a>` : `<div data-outbound-lookup="${row.id}">${outboundPhoneLookup(row)}</div>`}</td>
      <td><div class="outbound-stage-controls"><select data-outbound-status="${row.id}" class="outbound-status status-${row.status}" aria-label="Status for ${escapeHtml(row.name)}">${options(row.status)}</select><select data-outbound-method="${row.id}" class="outbound-method" aria-label="Contact method for ${escapeHtml(row.name)}" title="Method used to contact this prospect">${methodOptions(row.contact_method || "")}</select><select data-outbound-touchpoint="${row.id}" class="outbound-method" aria-label="Add touchpoint for ${escapeHtml(row.name)}"><option value="">+ Touchpoint</option>${methodOptions(null).replace(/<option value=""[^>]*>.*?<\/option>/, "")}</select></div>
        ${row.contacted_at ? `<small>Contacted ${escapeHtml(absoluteDate(row.contacted_at))}</small>` : ""}
        <div class="outbound-touchpoints">${(row.touchpoints || []).map(touch => `<small>${escapeHtml((data.contact_methods || {})[touch.contact_method] || touch.contact_method)} · ${escapeHtml(absoluteDate(touch.contacted_at))}</small>`).join("")}</div></td>
      <td class="muted outbound-date">${escapeHtml(absoluteDate(row.updated_at))}</td></tr>`;
    }).join("") || `
      <tr><td colspan="6" class="empty outbound-empty">${outboundStatus || state.pageSearch
        ? "<strong>No matches</strong><small>No saved people match these filters.</small>"
        : "<strong>No saved people yet</strong><small>Save a person from a company’s Contacts to start tracking outreach.</small>"}
      </td></tr>`;
    $("#outbound-range").textContent = data.total ? `${outboundOffset+1}–${Math.min(outboundOffset+outboundPageSize,data.total)} of ${formatNumber(data.total)}` : "0 people";
    $("#outbound-prev").disabled = outboundOffset === 0;
    $("#outbound-next").disabled = outboundOffset+outboundPageSize >= data.total;
    setPageSearchStatus(state.pageSearch ? `${formatNumber(data.total)} matches` : "");
    document.dispatchEvent(new CustomEvent("beings-page-status", {detail:{text:""}}));
  } catch (error) {
    if (generation === outboundGeneration && state.activeView === "prospects") {
      document.dispatchEvent(new CustomEvent("beings-page-status", {detail:{text:error.message}}));
      return;
    }
  }
}

let spendLoadGeneration = 0;
let spendRefreshTimer = null;
let spendPeriod = "all";
let spendSyncing = false;

function spendMoney(value) {
  if (value == null) return "—";
  return money(Number(value) / 1_000_000);
}

function spendShell() {
  spendPeriod = "all";
  $("#view-spend").innerHTML = `<div class="spend-page page-body">
    <div class="spend-sidebar">
    <section class="panel spend-metric spend-total"><span id="spend-total-label">Total Spend</span><strong id="spend-total-value">—</strong></section>
    <div class="spend-controls"><button class="button" id="spend-sync" type="button">Refresh</button>
      <p class="spend-status" id="spend-status" role="status">${loadingIndicator("Loading spend…")}</p></div></div>
    <div id="spend-content"></div></div>`;
  $("#spend-sync").disabled = spendSyncing;
  $("#spend-sync").addEventListener("click", syncSpend);
}

function actualSpendRows(data) {
  const services = [...data.services].sort((a, b) =>
    Number(a.service === "treg") - Number(b.service === "treg"));
  return services.map(service => {
    const openai = service.service === "openai";
    const hasReceipts = !openai && service.recorded_microusd > 0;
    // Direct billing/credit-usage reports supersede overlapping app receipts.
    // Reconstructed balances and token-price calculations are not actual charges.
    const reports = (data.provider_reports || []).filter(report => report.service === service.service
      && report.report_key !== "status" && report.status === "available"
      && (!openai || report.scope === "api_key")
      && ["provider_reported", "usage_credits"].includes(report.basis)
      && Number.isSafeInteger(report.amount_microusd));
    const report = reports[0];
    const status = (data.provider_reports || []).find(row => row.service === service.service
      && row.report_key === "status");
    const failed = openai && status?.status === "unavailable";
    const connectionStatus = {
      admin_key_required:"Admin key required", invalid_admin_key:"Billing key rejected",
      billing_access_denied:"Billing access denied", rate_limited:"Rate limited",
      research_key_required:"Research key required", research_key_unmatched:"Key match unavailable",
    }[status?.error_code] || "Refresh failed";
    return {name:service.name,
      complete:!failed && reports.length > 0 && reports.every(row=>row.coverage === "complete"),
      amount:reports.length ? reports.reduce((sum,row)=>sum+row.amount_microusd,0)
        : hasReceipts ? service.recorded_microusd : null,
      source:reports.length ? (openai ? "Research API key" : "Provider account") : hasReceipts ? "App receipts"
        : failed ? connectionStatus : "Unavailable",
      refreshFailed:failed && reports.length > 0,
      note:failed ? status.note : report?.note || "",
      url:report?.source_url || null,
      checked:reports.length ? reports.reduce((date,row)=>row.as_of > date ? row.as_of : date, "") : data.as_of};
  });
}
function renderSpend(data) {
  const rows = actualSpendRows(data);
  const known = rows.filter(row => row.amount !== null);
  const total = known.reduce((sum,row)=>sum+row.amount,0);
  $("#spend-total-value").textContent = known.length ? spendMoney(total) : "Unknown";
  const incomplete = known.length !== rows.length || known.some(row=>!row.complete);
  const label = $("#spend-total-label");
  if (label) label.textContent = known.length && incomplete ? "Known spend" : "Total Spend";
  $("#spend-content").innerHTML = `<section class="panel"><div class="table-wrap">
    <table class="spend-table spend-simple-table"><thead><tr><th>Service</th><th>Actual cost</th><th>Source</th><th class="spend-edit-cell"><span class="sr-only">Settings</span></th></tr></thead><tbody>
      ${rows.map(row => `<tr class="spend-service-row"><td title="${escapeHtml(row.source)} · Checked ${escapeHtml(absoluteDate(row.checked))}"><strong>${escapeHtml(row.name)}</strong></td>
        <td class="spend-amount">${row.amount === null ? '<span class="muted">Unknown</span>' : spendMoney(row.amount)}</td>
        <td title="${escapeHtml(row.note)}">${/^https?:\/\//i.test(row.url || "") ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.source)} ↗</a>` : escapeHtml(row.source)}${row.refreshFailed ? ' · Refresh failed' : ''}</td>
        <td class="spend-edit-cell"><button type="button" class="icon-button spend-edit" data-view="settings" aria-label="Edit ${escapeHtml(row.name)} settings"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 3 5 5-12 12-6 1 1-6Z"/><path d="m14 5 5 5"/></svg></button></td></tr>`).join("")}
    </tbody></table></div></section>`;
  $("#spend-status").textContent = [
    known.length && incomplete ? `Partial total · ${known.length}/${rows.length} services` : "",
    rows.some(row=>row.source === "Provider account") ? "Provider-account scope" : "",
  ].filter(Boolean).join(" · ");
  if (state.pageSearch) applyLocalPageSearch();
}

async function loadSpend() {
  clearTimeout(spendRefreshTimer);
  const generation = ++spendLoadGeneration;
  try {
    const data = await api(`/api/spend?period=${encodeURIComponent(spendPeriod)}`);
    if (generation !== spendLoadGeneration || state.activeView !== "spend") return;
    renderSpend(data);
  } catch (error) {
    if (generation === spendLoadGeneration && state.activeView === "spend")
      $("#spend-status").textContent = error.message;
  } finally {
    if (generation === spendLoadGeneration && state.activeView === "spend")
      spendRefreshTimer = setTimeout(loadSpend, 15000);
  }
}

async function syncSpend() {
  if (spendSyncing) return;
  spendSyncing = true;
  ++spendLoadGeneration;
  clearTimeout(spendRefreshTimer);
  $("#spend-sync").disabled = true;
  $("#spend-sync").textContent = "Refreshing…";
  $("#spend-status").textContent = "Reading billing reports; no research scans are started.";
  try {
    await api("/api/spend/sync", {method:"POST"});
    if (state.activeView === "spend") await loadSpend();
  } catch (error) {
    if (state.activeView === "spend") $("#spend-status").textContent = error.message;
  } finally {
    spendSyncing = false;
    if (state.activeView === "spend") {
      $("#spend-sync").disabled = false;
      $("#spend-sync").textContent = "Refresh";
      clearTimeout(spendRefreshTimer);
      spendRefreshTimer = setTimeout(loadSpend,15000);
    }
  }
}

async function refreshVisibleData() {
  invalidateTargetOverviewCache();
  invalidateSalesWorkbench();
  // These pages revalidate when revisited; background jobs do not fetch hidden inventories.
  for (const view of ["sources", "research", "runs"]) {
    if (view !== state.activeView) state.initializedViews.delete(view);
  }
  if (state.activeView === "sales") return loadSalesWorkbench({ invalidate: true });
  if (state.activeView === "overview") return loadDashboard();
  if (state.activeView === "prospects") return loadOutboundProspects();
  if (state.activeView === "accounts") return loadAccounts();
  if (state.activeView === "runs") return loadRuns();
  if (state.activeView === "spend") return loadSpend();
  if (state.activeView === "sources") return Promise.all([loadSources(), loadIntelligence()]);
}

async function setView(name, { preserveSearch = false } = {}) {
  document.dispatchEvent(new CustomEvent("beings-page-status", {detail:{text:""}}));
  if (needsSalesMission() && name !== "settings") name = "sales";
  if (name === "intelligence") {
    await setView("smart_scan", {preserveSearch});
    if (state.activeView !== "smart_scan") return;
    if (window.location.hash === "#intelligence") {
      window.history.replaceState(null, "", "#smart_scan");
    }
    state.openSmartScanWhenReady = true;
    renderScanLauncherStatus();
    return;
  }
  if (name !== "smart_scan") state.openSmartScanWhenReady = false;
  if (["sales", "pipeline", "accounts", "overview"].includes(name)) {
    setSalesMode(["pipeline", "accounts"].includes(name) ? "pipeline" : "prospects");
    name = "sales";
  }
  if (name === state.activeView && state.initializedViews.has(name)) {
    if (name === "sales") {
      window.history.replaceState(null, "", salesMode() === "pipeline" ? "#pipeline" : "#sales");
      updatePageSearchContext();
      await loadSalesWorkbench();
    }
    if (name === "overview") await loadDashboard();
    if (name === "prospects") await loadOutboundProspects();
    return;
  }
  clearTimeout(pageSearchDebounce);
  if (!preserveSearch) {
    state.pageSearch = "";
    state.filters.search = "";
    state.accountFilters.search = "";
    setPageSearchStatus("");
  }
  if (state.activeView === "sales" && name !== "sales") deactivateSalesWorkbench();
  state.activeView = name;
  renderAggregateScanState();
  if (name !== "prospects") ++outboundGeneration;
  if (name !== "spend") {
    ++spendLoadGeneration;
    clearTimeout(spendRefreshTimer);
  }
  if (!["smart_scan", "sources"].includes(name)) {
    setScoutActivityTicker(false);
    clearTimeout(state.scoutPollTimer);
    state.scoutPollTimer = null;
  }
  if (name !== "smart_scan") {
    clearTimeout(state.adDiscoveryPollTimer);
    state.adDiscoveryPollTimer = null;
  }
  if (name !== "smart_scan") {
    clearTimeout(state.smartScanPollTimer);
    state.smartScanPollTimer = null;
    clearInterval(state.smartScanElapsedTimer);
    state.smartScanElapsedTimer = null;
    clearInterval(state.smartScanActionTimer);
    state.smartScanActionTimer = null;
  }
  if (!["smart_scan", "sources"].includes(name)) {
    clearTimeout(state.discoveryPollTimer);
    state.discoveryPollTimer = null;
  }
  const navigationView = name === "sources" ? "settings" : name;
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === navigationView));
  $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  const titles = { sales: "Companies", smart_scan: "Scan", overview: "Targets", accounts: "Accounts", prospects: "Prospects", sources: "Sources", research: "Research", runs: "Scans", settings: "Settings", spend: "Spend" };
  $("#page-title").textContent = titles[name] || name; window.location.hash = name === "sales" && salesMode() === "pipeline" ? "pipeline" : name;
  updatePageSearchContext();
  const firstVisit = !state.initializedViews.has(name);
  if (!firstVisit) {
    if (name === "smart_scan") {
      startSmartScanElapsedTicker();
      await loadSmartScan();
    }
    if (["smart_scan", "sources"].includes(name) && activeDiscoveryRuns().length) pollDiscoveryMethodStatus();
    if (name === "smart_scan" && adDiscoveryRunActive()) pollAdDiscoveryStatus();
    if (["smart_scan", "sources"].includes(name) && state.intelligence?.scout_status?.status === "running") pollSourceScout();
    if (name === "spend") await loadSpend();
    if (name === "sales") await loadSalesWorkbench();
    if (name === "overview") await loadDashboard();
    if (name === "prospects") await loadOutboundProspects();
    if (state.pageSearch) applyLocalPageSearch();
    return;
  }
  if (name === "spend") { spendShell(); await loadSpend(); }
  if (name === "smart_scan") { smartScanViewShell(); await loadSmartScan(); }
  if (name === "sales") await loadSalesWorkbench();
    if (name === "overview") await loadDashboard();
  if (name === "accounts") { accountsShell(); await loadAccounts(); }
  if (name === "prospects") { outboundShell(); await loadOutboundProspects(); }
  if (name === "sources") { sourcesShell(); await Promise.all([loadSources({ useCached: true }), loadIntelligence({ useCached: true })]); }
  if (name === "research") { researchShell(); await loadResearch(); }
  if (name === "runs") { runsShell(); await loadRuns(); }
  if (name === "settings") {
    settingsShell();
    await Promise.all([
      loadScanReadiness(),
      loadSecretSettings(),
      loadOpenAIBillingSettings(),
      loadBuyerProviderSettings(),
      loadApifySecretSettings(),
      loadMicrolinkSecretSettings(),
      loadPeopleHydrationStatus(),
      loadDataForSEOSecretSettings(),
    ]);
  }
  if (["sales", "prospects", "smart_scan", "overview", "sources", "intelligence", "settings", "spend"].includes(name)) state.initializedViews.add(name);
  if (state.activeView === name && state.pageSearch) applyLocalPageSearch();
}

function backdropDialog(event) {
  const dialog = event.target;
  if (!(dialog instanceof HTMLDialogElement) || !dialog.matches(":modal")) return null;
  const {left, right, top, bottom} = dialog.getBoundingClientRect();
  const {clientX, clientY} = event;
  return clientX < left || clientX >= right || clientY < top || clientY >= bottom ? dialog : null;
}

// Both ends of a backdrop click must be outside the same modal.
let pressedBackdrop = null;
document.addEventListener("pointerdown", event => {
  if (event.isPrimary) pressedBackdrop = event.button === 0 ? backdropDialog(event) : null;
}, true);
document.addEventListener("pointercancel", () => { pressedBackdrop = null; }, true);
document.addEventListener("click", event => {
  const dialog = pressedBackdrop;
  pressedBackdrop = null;
  if (!dialog || event.detail === 0 || backdropDialog(event) !== dialog) return;
  event.stopPropagation();
  // Reuse the Escape path so form and operation guards can veto dismissal.
  if (typeof dialog.requestClose === "function") dialog.requestClose();
  else if (dialog.dispatchEvent(new Event("cancel", {cancelable: true}))) dialog.close();
}, true);

document.addEventListener("click", async event => {
  const dialogClose = event.target.closest("[data-close-dialog]");
  if (dialogClose) document.getElementById(dialogClose.dataset.closeDialog)?.close();
  const viewButton = event.target.closest("[data-view], [data-view-target]");
  if (viewButton) {
    if (viewButton.dataset.targetLane) state.filters.lane = viewButton.dataset.targetLane;
    if (viewButton.dataset.targetScope) state.filters.scope = viewButton.dataset.targetScope;
    setView(viewButton.dataset.view || viewButton.dataset.viewTarget);
  }
  const targetSort = event.target.closest("[data-target-sort]");
  if (targetSort) {
    event.stopPropagation();
    const sort = targetSort.dataset.targetSort;
    if (targetSort.closest("#view-prospects")) {
      state.filters.sort = sort;
      state.prospectOffset = 0;
      if ($("#target-sort")) $("#target-sort").value = sort;
      syncTargetSortHeaders($("#view-prospects"), sort);
      await loadProspects();
    } else {
      state.overviewSort = sort;
      syncTargetSortHeaders($("#view-overview"), sort);
      await loadDashboard();
    }
  }
  const accountRow = event.target.closest("[data-account-id]");
  if (accountRow && !event.target.closest(".quick-open")) openAccount(accountRow.dataset.accountId);
  const findEmails = event.target.closest("[data-find-company-emails]");
  if (findEmails) {
    event.stopPropagation();
    findEmails.disabled = true;
    try {
      const id = Number(findEmails.dataset.findCompanyEmails);
      const result = await api(`/api/sales-engine/companies/${id}/emails`, {
        method: "POST", body: JSON.stringify({confirmed_maximum_microusd: 50000}),
      });
      if (findEmails.isConnected && $("#prospect-drawer").classList.contains("open")) await openProspect(id);
      showToast(result.message);
    } catch (error) { showToast(error.message, "error"); }
    finally { findEmails.disabled = false; }
    return;
  }
  const copyEmail = event.target.closest("[data-copy-email]");
  if (copyEmail) {
    event.stopPropagation();
    event.preventDefault();
    if (copyEmail.dataset.copyPending === "true") return;
    copyEmail.dataset.copyPending = "true";
    const original = copyEmail.innerHTML;
    try {
      await copyText(copyEmail.dataset.copyEmail);
      copyEmail.textContent = "Copied!";
      copyEmail.setAttribute("aria-live", "polite");
      setTimeout(() => {
        if (copyEmail.isConnected) copyEmail.innerHTML = original;
        delete copyEmail.dataset.copyPending;
      }, 1600);
    } catch (error) {
      delete copyEmail.dataset.copyPending;
      showToast("Could not copy email. Please try again.", "error");
    }
    return;
  }
  const row = event.target.closest("[data-prospect-id]");
  if (row && !event.target.closest("[data-star], [data-exclude], .quick-open, .contact-action")) openProspect(Number(row.dataset.prospectId));
  const star = event.target.closest("[data-star]");
  if (star) {
    event.stopPropagation(); const item = state.prospects.find(value => value.candidate_id === Number(star.dataset.star)); if (!item) return;
    item.starred = !item.starred; await api(`/api/prospects/${item.candidate_id}/workflow`, { method: "PATCH", body: JSON.stringify({ starred: item.starred }) }); await loadProspects();
  }
  const exclusion = event.target.closest("[data-exclude]");
  if (exclusion) {
    event.stopPropagation();
    exclusion.disabled = true;
    const candidateId = Number(exclusion.dataset.exclude);
    const excluded = exclusion.dataset.excluded !== "true";
    try {
      await api(`/api/prospects/${candidateId}/workflow`, { method: "PATCH", body: JSON.stringify({ excluded }) });
      if (excluded) exclusion.closest(".similar-target-row, .change-item")?.remove();
      showToast(excluded ? "Company excluded" : "Company restored");
      await refreshVisibleData();
    } catch (error) {
      exclusion.disabled = false;
      showToast(error.message, "error");
    }
  }
  const toggle = event.target.closest("[data-source-toggle]");
  if (toggle) {
    await api(`/api/sources/${encodeURIComponent(toggle.dataset.sourceToggle)}`, { method: "PATCH", body: JSON.stringify({ enabled: toggle.checked }) });
    const source = state.sources.find(value => value.apex === toggle.dataset.sourceToggle); if (source) source.enabled = toggle.checked;
    showToast(`${toggle.dataset.sourceToggle} ${toggle.checked ? "enabled" : "disabled"}`);
  }
  const sourceSort = event.target.closest("[data-source-sort]");
  if (sourceSort) {
    const key = sourceSort.dataset.sourceSort;
    const sameColumn = state.sourceSort.key === key;
    const defaultDirection = key === "apex" ? "asc" : "desc";
    state.sourceSort = {
      key,
      direction: sameColumn ? (state.sourceSort.direction === "asc" ? "desc" : "asc") : defaultDirection,
    };
    renderSources();
  }
  const openMethodTab = event.target.closest("[data-open-method-tab]");
  if (openMethodTab) {
    const tabId = openMethodTab.dataset.openMethodTab;
    state.discoveryTab = tabId;
    await setView("intelligence");
    selectDiscoveryTab(tabId, { focus: true });
  }
  const hydrateSource = event.target.closest("[data-hydrate-source]");
  if (hydrateSource) {
    hydrateSource.disabled = true;
    try {
      const payload = await api(`/api/intelligence/suggestions/${hydrateSource.dataset.hydrateSource}/hydrate`, {
        method: "POST",
        body: JSON.stringify({ max_network_inspections: 100, max_ai_calls: 50, max_ai_web_calls: 8 }),
      });
      if (["running", "retrying"].includes(String(payload.status || "").toLowerCase())) {
        state.webScanJobId = payload.job_id;
        state.scanStatus = payload.status;
        state.webScanStatus = payload;
        showToast(`Hydrating ${formatNumber(payload.hosts_requested || 0)} source-linked companies with AI`);
        pollScanStatus();
      } else {
        showToast(payload.message || "Source-linked companies are already hydrated");
        await loadSources();
      }
    } catch (error) {
      hydrateSource.disabled = false;
      showToast(error.message, "error");
    }
  }
  const dismissSource = event.target.closest("[data-dismiss-source]");
  if (dismissSource) {
    event.stopPropagation();
    dismissSource.disabled = true;
    const sourceId = String(dismissSource.dataset.dismissSource);
    state.pendingSourceDismissals.add(sourceId);
    renderDiscoverySourceInventory();
    renderAcceptedSourceInventory();
    try {
      const payload = await api(`/api/intelligence/suggestions/${sourceId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "rejected", activate: false }),
      });
      showToast(payload.cancelled_run_ids?.length ? "Source cancelled and dismissed permanently" : "Source dismissed permanently");
      await Promise.all([loadSources(), loadIntelligence(), loadDiscoveryMethods()]);
      state.pendingSourceDismissals.delete(sourceId);
    } catch (error) {
      state.pendingSourceDismissals.delete(sourceId);
      renderDiscoverySourceInventory();
      renderAcceptedSourceInventory();
      showToast(error.message, "error");
    }
  }
  const suggestionAction = event.target.closest("[data-suggestion-action]");
  if (suggestionAction) {
    suggestionAction.disabled = true;
    try {
      const suggestion = (state.intelligence?.suggestions || []).find(item => String(item.id) === suggestionAction.dataset.suggestionId);
      const payload = await api(`/api/intelligence/suggestions/${suggestionAction.dataset.suggestionId}`, { method: "PATCH", body: JSON.stringify({ status: suggestionAction.dataset.suggestionAction }) });
      const activation = payload?.activation || {};
      const activationState = String(activation.state || "").toLowerCase().replaceAll("-", "_");
      if (suggestionAction.dataset.suggestionAction === "accepted") {
        showToast(sourceActivationToast(activation, suggestion || payload));
        if (ACTIVE_RUN_STATES.has(activationState)) {
          const run = {
            id: activation.run_id,
            run_id: activation.run_id,
            method: activation.method || suggestion?.execution_method,
            status: activationState,
            message: activation.message,
          };
          const key = String(run.id || `${run.method}:${run.status}`);
          state.discoveryRuns = [...activeDiscoveryRuns().filter(item => String(item.id || item.run_id || `${item.method}:${item.status}`) !== key), run];
        }
      } else {
        showToast(`Suggestion ${suggestionAction.dataset.suggestionAction}`);
      }
      if(payload.status === "accepted") {
        if(!applySourceReview(payload,suggestion))await loadIntelligence();
        if(payload.source?.source_kind === "tenant_apex")await loadSources();
        if(state.activeView === "smart_scan")await loadDiscoveryMethods();
      } else {
        await Promise.all([loadIntelligence(), loadSources(), loadDiscoveryMethods()]);
      }
      if (suggestionAction.dataset.suggestionAction === "accepted" && ACTIVE_RUN_STATES.has(activationState)) pollDiscoveryMethodStatus();
    } catch (error) {
      suggestionAction.disabled = false;
      showToast(error.message, "error");
      // A failed mutation leaves the current view intact; revalidate only visible source data.
      if(state.activeView === "sources")void loadSources();
      if(state.activeView === "smart_scan")void loadIntelligence();
    }
  }
  const adLinkAction = event.target.closest("[data-ad-link-action]");
  if (adLinkAction) {
    const action = adLinkAction.dataset.adLinkAction;
    if (!window.confirm(`${action === "verified" ? "Verify" : "Reject"} this advertiser-to-domain mapping?`)) return;
    const reviewNote = window.prompt("Review note (optional)", "");
    if (reviewNote == null) return;
    adLinkAction.disabled = true;
    await updateAdDiscoveryLink(adLinkAction.dataset.adLinkId, action, reviewNote.trim());
  }
  const copyQuery = event.target.closest("[data-copy-query]");
  if (copyQuery) { await copyText(copyQuery.dataset.copyQuery); showToast("Query copied"); }
  const resume = event.target.closest("[data-resume-run]");
  if (resume) {
    if (scanActionBlocked(resume)) return;
    resume.disabled = true;
    try {
      const result = await api(`/api/runs/${resume.dataset.resumeRun}/resume`, { method: "POST" });
      showToast(`Scan ${resume.dataset.resumeRun} resumed · ${formatNumber(result.retrying_count || 0)} failed hosts`);
      await loadRuns();
      pollScanStatus();
    } catch (error) {
      resume.disabled = false;
      showToast(error.message, "error");
    }
  }
  const deleteClaim = event.target.closest("[data-delete-claim]");
  if (deleteClaim && state.activeAccount) {
    deleteClaim.disabled = true;
    try {
      const refreshed = unwrapAccount(await api(`/api/accounts/${encodeURIComponent(state.activeAccount.id)}/claims/${encodeURIComponent(deleteClaim.dataset.deleteClaim)}`, { method: "DELETE" }));
      showToast("Evidence claim deleted");
      renderAccountWorkspace(refreshed);
      await refreshAccountViews();
    } catch (error) { deleteClaim.disabled = false; showToast(error.message, "error"); }
  }
  const verifyClaim = event.target.closest("[data-verify-claim]");
  if (verifyClaim && state.activeAccount) {
    const verifiedBy = window.prompt("Who verified this claim?");
    if (verifiedBy?.trim()) {
      verifyClaim.disabled = true;
      try {
        const refreshed = unwrapAccount(await api(`/api/accounts/${encodeURIComponent(state.activeAccount.id)}/claims/${encodeURIComponent(verifyClaim.dataset.verifyClaim)}`, { method: "PATCH", body: JSON.stringify({ human_verified: true, verified_by: verifiedBy.trim(), verified_at: new Date().toISOString() }) }));
        showToast("Evidence claim verified");
        renderAccountWorkspace(refreshed);
        await refreshAccountViews();
      } catch (error) { verifyClaim.disabled = false; showToast(error.message, "error"); }
    }
  }
});

$("#drawer-backdrop").addEventListener("click", closeDrawer);
$("#account-backdrop").addEventListener("click", closeAccountWorkspace);
$("#source-form").addEventListener("submit", async event => {
  event.preventDefault(); const form = new FormData(event.currentTarget);
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify({ apex: form.get("apex"), category: form.get("category"), notes: form.get("notes") || "Added from dashboard" }) });
    $("#source-modal").close(); event.currentTarget.reset(); showToast("Source added"); await loadSources();
  } catch (error) { showToast(error.message, "error"); }
});
$("#new-account-segment").addEventListener("change", event => {
  if (event.target.value === "agency_buyer") $("#new-account-relationship").value = "buyer";
  if (event.target.value === "agency_referral_partner") $("#new-account-relationship").value = "referral_partner";
});
$("#account-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = Object.fromEntries(new FormData(form));
  if (body.segment === "agency_buyer") body.relationship_type = "buyer";
  if (body.segment === "agency_referral_partner") body.relationship_type = "referral_partner";
  try {
    const payload = await api("/api/accounts", { method: "POST", body: JSON.stringify(body) });
    const account = unwrapAccount(payload);
    $("#account-modal").close(); form.reset(); form.elements.cohort.value = "prototype-v1"; form.elements.lead_source.value = "targeted_outbound";
    showToast(payload.created === false ? "Existing company opened" : "Company added to pipeline");
    await refreshAccountViews();
    if (account?.id) await openAccount(account.id);
  } catch (error) { showToast(error.message, "error"); }
});

async function readAccountImport() {
  const file = $("#account-import-form").elements.file.files[0];
  if (!file) throw new Error("Choose a CSV file first");
  state.accountImportText = await file.text();
  if (!state.accountImportText.trim()) throw new Error("The CSV file is empty");
  return state.accountImportText;
}

function renderImportResult(result) {
  const errors = result.errors || [];
  const rows = result.rows || [];
  const total = Number(result.total ?? rows.length);
  const validRows = typeof result.valid === "boolean" ? (result.valid ? total : 0) : Number(result.valid || 0);
  $("#account-import-preview").innerHTML = `<div class="import-summary"><strong>${formatNumber(total)} rows</strong><span>${formatNumber(validRows)} valid · ${formatNumber(result.existing ?? 0)} existing · ${formatNumber(result.created ?? 0)} created</span></div>${errors.length ? `<ul class="import-errors">${errors.slice(0, 20).map(error => `<li>${escapeHtml(typeof error === "string" ? error : `Row ${error.row || "?"}: ${error.message || error.error || JSON.stringify(error)}`)}</li>`).join("")}</ul>` : `<p class="success-note">Valid.</p>`}`;
  $("#commit-account-import").disabled = Boolean(errors.length) || validRows === 0;
}

$("#preview-account-import").addEventListener("click", async () => {
  try {
    const csv = await readAccountImport();
    const result = await api("/api/accounts/import.csv?dry_run=true", { method: "POST", headers: { "Content-Type": "text/csv" }, body: csv });
    renderImportResult(result);
    showToast("CSV validation complete");
  } catch (error) { showToast(error.message, "error"); }
});
$("#account-import-form").elements.file.addEventListener("change", () => {
  state.accountImportText = "";
  $("#commit-account-import").disabled = true;
  $("#account-import-preview").replaceChildren();
});
$("#account-import-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const csv = state.accountImportText || await readAccountImport();
    const result = await api("/api/accounts/import.csv?dry_run=false", { method: "POST", headers: { "Content-Type": "text/csv" }, body: csv });
    renderImportResult(result);
    $("#account-import-modal").close(); event.currentTarget.reset(); state.accountImportText = ""; $("#commit-account-import").disabled = true;
    showToast(`${formatNumber(result.created || 0)} accounts imported`);
    await refreshAccountViews();
  } catch (error) { showToast(error.message, "error"); }
});

let pageSearchDebounce;
$("#page-search-form").addEventListener("submit", event => {
  event.preventDefault();
  clearTimeout(pageSearchDebounce);
  state.pageSearch = $("#page-search").value;
  executePageSearch();
});
$("#page-search").addEventListener("input", event => {
  clearTimeout(pageSearchDebounce);
  state.pageSearch = event.target.value;
  pageSearchDebounce = setTimeout(executePageSearch, 250);
});

let pageSearchRenderFrame;
const pageSearchObserver = new MutationObserver(() => {
  if (!state.pageSearch || ["overview", "prospects", "accounts"].includes(state.activeView)) return;
  cancelAnimationFrame(pageSearchRenderFrame);
  pageSearchRenderFrame = requestAnimationFrame(applyLocalPageSearch);
});
$$('.view').forEach(view => pageSearchObserver.observe(view, { childList: true, subtree: true }));

function sidebarShortcutItems() {
  return $$(".sidebar .nav-item").filter(button =>
    !button.disabled && !button.closest("[hidden]") && button.getClientRects().length > 0);
}
function syncSidebarShortcuts() {
  sidebarShortcutItems().forEach((button, index) => {
    const key = String(index + 1);
    const badge = button.querySelector("kbd");
    if (badge && badge.textContent !== key) badge.textContent = key;
    if (button.getAttribute("aria-keyshortcuts") !== key) button.setAttribute("aria-keyshortcuts", key);
  });
}
syncSidebarShortcuts();
const sidebarShortcutObserver = new MutationObserver(syncSidebarShortcuts);
sidebarShortcutObserver.observe($(".sidebar"), {subtree:true, childList:true, attributes:true, attributeFilter:["hidden", "style", "class", "disabled"]});
window.addEventListener("resize", syncSidebarShortcuts);

document.addEventListener("keydown", event => {
  browseCompanyKey(event);
  if (event.key === "Escape") { closeDrawer(); closeAccountWorkspace(); }
  const target = event.target;
  const isEditing = target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
  if (!/^[1-9]$/.test(event.key) || isEditing || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
  const item = sidebarShortcutItems()[Number(event.key) - 1];
  if (!item) return;
  event.preventDefault();
  event.stopPropagation();
  item.click();
}, { capture: true });

function consumeRefreshState() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(REFRESH_STATE_KEY) || "null");
    sessionStorage.removeItem(REFRESH_STATE_KEY);
    return saved && typeof saved === "object" ? saved : null;
  } catch {
    return null;
  }
}

function saveRefreshLocation() {
  try {
    const view = document.querySelector(`#view-${state.activeView}`);
    sessionStorage.setItem(REFRESH_STATE_KEY, JSON.stringify({
      activeView: state.activeView === "sales" && salesMode() === "pipeline" ? "pipeline" : state.activeView,
      discoveryTab: state.discoveryTab,
      pageSearch: state.pageSearch,
      viewScroll: view?.scrollTop || 0,
      sales: captureSalesLocation(),
    }));
  } catch {
    // The URL hash still preserves the primary tab when session storage is unavailable.
  }
}
window.addEventListener("pagehide", saveRefreshLocation);
$("#refresh-app").addEventListener("click", () => {
  saveRefreshLocation();
  window.location.reload();
});

configureSalesWorkbench({
  scanStarted: result => {
    state.webScanJobId = result.job_id || null;
    void pollScanStatus();
  },
  notify: showToast,
  onboardingChanged: required => {
    $$(".sidebar .nav > button[data-view]:not([data-view='sales'])").forEach(button => {
      if (button.hidden !== required) button.hidden = required;
    });
  },
  scanCredentialsChanged: () => loadScanReadiness({checkTregHealth:false}),
  editProspect: editOutboundProspect,
  contactLookupChanged: syncOutboundLookupActions,
  prospectChanged: () => {
    if (state.activeView === "prospects") void loadOutboundProspects();
  },
  navigate: name => setView(name),
  import: () => $("#account-import-modal").showModal(),
  export: path => downloadPath(path || "/api/accounts/export.csv?cohort="),
});
window.addEventListener("hashchange", () => {
  const route = window.location.hash.slice(1);
  const current = state.activeView === "sales" && salesMode() === "pipeline" ? "pipeline" : state.activeView;
  if (route !== current && ["sales","pipeline","accounts","overview","prospects","smart_scan","sources","intelligence","settings","spend"].includes(route)) void setView(route);
});

async function bootstrap() {
  if (document.documentElement.hasAttribute("data-require-user-mission")) {
    await loadSalesMissions().catch(error => showToast(error.message, "error"));
  }
  const restored = consumeRefreshState();
  if (restored?.discoveryTab && (
    DISCOVERY_TAB_DEFINITIONS.some(tab => tab.id === restored.discoveryTab)
    || SCAN_SECTION_LINKS.some(([id]) => id === restored.discoveryTab)
  )) state.discoveryTab = restored.discoveryTab;
  const salesRestored = restoreSalesLocation(restored?.sales);
  if (typeof restored?.pageSearch === "string") state.pageSearch = restored.pageSearch;
  const initial = restored?.activeView || window.location.hash.slice(1) || "sales";
  const supported = ["sales", "pipeline", "accounts", "smart_scan", "overview", "prospects", "sources", "intelligence", "settings", "spend"];
  await setView(supported.includes(initial) ? initial : "sales", {preserveSearch:Boolean(restored)});
  if (restored) {
    if (salesRestored && state.activeView === "sales") restoreSalesScroll(restored.sales);
    const view = document.querySelector(`#view-${state.activeView}`);
    if (view && Number.isFinite(restored.viewScroll) && restored.viewScroll >= 0) view.scrollTop = restored.viewScroll;
  }
  if (state.activeView !== "settings") void loadScanReadiness({checkTregHealth:false});
  // The visible company list/summary wins the initial request budget. This tiny status
  // powers the global progress strip; research and other inventories load on demand.
  if (!needsSalesMission()) void pollScanStatus();
}

normalizeDisplayedTerminology();
const terminologyObserver = new MutationObserver(records => {
  records.forEach(record => {
    if (record.target.parentElement?.closest("#view-sales, #account-workspace, [data-verbatim]")) return;
    if (record.type === "characterData") {
      const normalized = scanTerminology(record.target.nodeValue);
      if (normalized !== record.target.nodeValue) record.target.nodeValue = normalized;
      return;
    }
    record.addedNodes.forEach(node => {
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.parentElement?.closest("[data-verbatim]")) return;
        const normalized = scanTerminology(node.nodeValue);
        if (normalized !== node.nodeValue) node.nodeValue = normalized;
      } else if (node.nodeType === Node.ELEMENT_NODE) normalizeDisplayedTerminology(node);
    });
  });
});
terminologyObserver.observe(document.body, { childList: true, subtree: true, characterData: true });

bootstrap();
if (!document.documentElement.hasAttribute("data-require-user-mission")) {
  void loadSalesMissions().catch(error => showToast(error.message, "error"));
}

const interfaceThemeButton = document.getElementById("interface-theme");
const themeColorMeta = document.querySelector('meta[name="theme-color"]');
function applyInterfaceTheme(light) {
  document.documentElement.dataset.theme = light ? "light" : "dark";
  interfaceThemeButton.setAttribute("aria-pressed", String(light));
  themeColorMeta.content = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim();
}

const themeRoot = document.documentElement;
const reduceThemeMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let lightInterface = themeRoot.dataset.theme === "light";
let themeSwitchId = 0;
let themeTransition;
applyInterfaceTheme(lightInterface);

interfaceThemeButton.addEventListener("click", () => {
  const light = lightInterface = !lightInterface;
  const switchId = ++themeSwitchId;
  themeTransition?.skipTransition();

  const apply = () => {
    if (switchId !== themeSwitchId) return;
    themeRoot.setAttribute("data-theme-switching", "");
    applyInterfaceTheme(light);
    try {
      localStorage.setItem("beings-interface-theme", themeRoot.dataset.theme);
    } catch {
      // The current page can still switch themes without storage.
    }
  };
  const finish = () => {
    if (switchId !== themeSwitchId) return;
    themeRoot.removeAttribute("data-theme-switching");
    themeTransition = undefined;
  };

  if (document.startViewTransition && !reduceThemeMotion.matches) {
    themeTransition = document.startViewTransition(apply);
    // An interrupted transition rejects ready, but still runs its update callback.
    themeTransition.ready.catch(() => {});
    themeTransition.finished.then(finish, finish);
  } else {
    apply();
    requestAnimationFrame(() => requestAnimationFrame(finish));
  }
});

// Native controls share the View menu's page zoom, independent of website zoom.
let browserInterfaceZoom = 1;
document.querySelectorAll("[data-interface-zoom]").forEach(button => {
  button.addEventListener("click", () => {
    const action = button.dataset.interfaceZoom;
    const bridge = window.webkit?.messageHandlers?.interfaceZoom;
    if (bridge) {
      bridge.postMessage(action);
      return;
    }
    browserInterfaceZoom = Math.max(0.5, Math.min(3,
      Math.round((browserInterfaceZoom + (action === "in" ? 0.1 : -0.1)) * 10) / 10));
    document.documentElement.style.zoom = browserInterfaceZoom;
    window.dispatchEvent(new Event("resize"));
  });
});

// Delegated help survives company-detail rerenders without accumulating listeners.
function positionFitReviewHelp() {
  const tip = document.querySelector("#fit-review-help:popover-open");
  const trigger = document.querySelector("[data-fit-review-help]");
  if (!tip || !trigger) return;
  const box = trigger.getBoundingClientRect();
  const width = document.documentElement.clientWidth;
  const height = document.documentElement.clientHeight;
  const below = height - box.bottom - 18;
  const above = box.top - 18;
  tip.style.maxHeight = `${Math.max(0, Math.max(above, below))}px`;
  tip.style.left = `${Math.max(12, Math.min(box.left, width - tip.offsetWidth - 12))}px`;
  tip.style.top = `${below >= tip.offsetHeight ? box.bottom + 6
    : Math.max(12, box.top - tip.offsetHeight - 6)}px`;
}
let fitReviewHelpTimer;
for (const eventName of ["pointerover", "focusin"]) document.addEventListener(eventName, event => {
  if (!event.target.closest?.("[data-fit-review-help], #fit-review-help")) return;
  if (eventName === "pointerover" && event.pointerType !== "mouse") return;
  if (eventName === "focusin" && !event.target.matches(":focus-visible")) return;
  clearTimeout(fitReviewHelpTimer);
  const tip = document.querySelector("#fit-review-help");
  if (tip && !tip.matches(":popover-open")) tip.showPopover();
  positionFitReviewHelp();
});
for (const eventName of ["pointerout", "focusout"]) document.addEventListener(eventName, event => {
  if (!event.target.closest?.("[data-fit-review-help], #fit-review-help")) return;
  if (event.relatedTarget?.closest?.("[data-fit-review-help], #fit-review-help")) return;
  fitReviewHelpTimer = setTimeout(() => {
    const tip = document.querySelector("#fit-review-help:popover-open");
    if (!document.activeElement?.matches("[data-fit-review-help]")) tip?.hidePopover();
  }, 180);
});
document.addEventListener("toggle", event => {
  if (event.target.id === "fit-review-help") positionFitReviewHelp();
}, true);
window.addEventListener("resize", positionFitReviewHelp);
document.addEventListener("scroll", positionFitReviewHelp, true);
document.addEventListener("click", event => {
  if (!event.target.closest?.("[data-fit-review-help]")) return;
  event.preventDefault();
  const tip = document.querySelector("#fit-review-help");
  if (tip && !tip.matches(":popover-open")) tip.showPopover();
  positionFitReviewHelp();
});
