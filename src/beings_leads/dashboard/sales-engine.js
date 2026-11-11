import {createCompanyWebsite} from "./company-website.js?v=11";
let companyWebsite;
let scoreDialog;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = () => new Date().toISOString();
const date = value => value ? new Date(value).toLocaleDateString(undefined, {timeZone:"UTC"}) : "Unknown";
function manualRunDate(value) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    month:"numeric", day:"numeric", year:"2-digit", hour:"numeric", minute:"2-digit", hour12:true,
  }).formatToParts(new Date(value)).map(part => [part.type, part.value]));
  return `${parts.month}/${parts.day}/${parts.year} ${parts.hour}:${parts.minute}${parts.dayPeriod}`;
}
const link = (url, label) => /^https?:\/\//i.test(url || "") ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label || url)}</a>` : esc(label || "");
const button = (text, action, extra = "", classes = "button") => `<button type="button" class="${classes}" data-engine="${action}" ${extra}>${text}</button>`;
const tagLabel = tag => tag === "starred" ? "Starred" : tag;
let companyTags = [];
let selectedTag = "";
let tagMutation = false;
let missionResearchPending = false;
const companyTagSaves = new Set();
let state = {tab:"queue", overview:null, packet:null, search:"", message:""};
const emailLookupStages = new Map();
const userMissionRequired = document.documentElement?.hasAttribute?.("data-require-user-mission");
const vmDashboard = document.documentElement?.getAttribute?.("data-storage-mode") === "vm";
export const VM_SETUP_GUIDE = "https://github.com/cameoed/growth/blob/main/deploy/oracle/README.md";
const loadingCompanies = vmDashboard ? "Loading in your company list from your virtual machine" : "Loading companies";
const onboardingSteps = {storage:"Storage", brief:"Target", key:"OpenAI", review:"Review"};
let onboardingStep = "storage";
let onboardingStorageHelp = "";
let onboardingDraft = {name:"My mission", brief:"", model:""};
let onboardingKeyConfigured = false;
let missionId = userMissionRequired ? null : "beings";
let missions = userMissionRequired ? [] : [{id:"beings",name:"Beings",archived:0}];
export function needsSalesMission() {
  return userMissionRequired && !missions.some(mission => !mission.archived);
}
let missionMutation = false;
let missionDialogView = null;
let compilerModels = [{id:"gpt-5.6-sol",maximum_microusd:200000,reasoning_effort:"medium"},{id:"gpt-6-astra",maximum_microusd:500000,reasoning_effort:"medium"},{id:"gpt-6.1-sol",maximum_microusd:200000,reasoning_effort:"high"}];
let defaultCompilerModel = "gpt-5.6-sol";
const missionRoles = {auto:"Auto", buyer:"Buyers", seller:"Sellers", trader:"Buyers or sellers", matching_company:"Companies doing this activity"};
const missionFocuses = {auto:"Auto", fit:"Company fit", recurring_activity:"Recurring activity", timing:"Timely signals", scale:"Commercial scale", access:"Reachable contacts"};
function missionSetupOptions() {
  const field = (name, label, choices) => `<label class="engine-mission-field"><span>${label}</span><select name="${name}">${Object.entries(choices).map(([value, text])=>`<option value="${value}">${text}</option>`).join("")}</select></label>`;
  return `<div class="engine-mission-setup"><div class="engine-mission-setup-options">${field("target_role", "Target role", missionRoles)}${field("ranking_focus", "Ranking focus", missionFocuses)}</div><small>Auto lets AI choose from your brief. Use “must” for requirements and “prefer” for priorities. AI also chooses relevant contact roles.</small></div>`;
}
function compilerOptions(selectedModel = defaultCompilerModel, compact = false) {
  const options = compilerModels.map(model => {
    const cost = `$${(model.maximum_microusd / 1000000).toFixed(2)} max`;
    const reasoning = model.reasoning_effort ? ` · ${model.reasoning_effort} reasoning` : "";
    const label = compact ? `${cost} · ${model.id}` : `${model.id}${reasoning} · ${cost} per attempt`;
    return `<option value="${esc(model.id)}" ${model.id === selectedModel ? "selected" : ""}>${esc(label)}</option>`;
  });
  return `<label class="engine-mission-field"><span id="engine-mission-model-label">AI model</span><select name="compiler_model" aria-labelledby="engine-mission-model-label" aria-describedby="engine-mission-model-note">${options.join("")}</select><small id="engine-mission-model-note">Analysis run once to map your mission to the company list.</small></label>`;
}
export function compilationErrorText(compilation) {
  const errors = {configure_openai_key:"Add an OpenAI API key in Settings to finish building this mission.",provider_authentication_failed:"OpenAI rejected the API key. Update it in Settings.",research_budget_exceeded:"Research budget is exhausted.",provider_rate_limited:"AI setup hit a provider rate or quota limit.",provider_quota_exceeded:"AI setup hit the provider's billing or usage limit.",cost_reconciliation_required:"AI setup needs cost reconciliation; no retry was sent.",invalid_or_incomplete_rubric:"AI returned an invalid or incomplete rubric.",invalid_rubric:"AI returned a rubric that failed validation.",invalid_rubric_normalization:"AI returned unsupported activity criteria.",incomplete_rubric:"AI did not finish the rubric.",rubric_refused:"AI declined to compile the mission.",compiler_processing_failed:"The compiler could not process the AI response.",mission_changed:"Mission changed before the rubric could be published."};
  return errors[compilation?.error_code] || "";
}
function compilationMessage(value) {
  if(value.compilation?.status === "ready") {
    const focus = value.spec?.setup?.ranking_focus;
    return focus && !["auto", "access"].includes(focus) && !value.spec.focused_criteria?.length
      ? "Mission ready. The selected ranking focus has no supported evidence rule for this brief."
      : "";
  }
  const text=compilationErrorText(value.compilation);
  if(!text)return "Mission saved; AI compilation is pending.";
  const ranking=value.scoring_available === false || value.spec?.compiler_pending ? "Mission remains unscored." : "Current ranking was preserved.";
  return `${text} ${ranking}${value.compilation?.can_retry ? " Use Retry mission with AI for a new budgeted attempt." : ""}`;
}
try {
  const savedMission = localStorage.getItem("beings-mission");
  if (savedMission && (!userMissionRequired || savedMission !== "beings")) missionId = savedMission;
} catch { /* Session only. */ }
function currentMissionTitle() {
  return missionId === "all" ? "All companies" : missions.find(m => m.id === missionId)?.name;
}
function missionControls() {
  const disabled=missionMutation ? "disabled" : "";
  const name = currentMissionTitle() || (needsSalesMission() ? "Choose a mission" : "Loading…");
  return `<button type="button" class="engine-mission-switch" data-engine="mission-manage" aria-label="Switch mission: ${esc(name)}" title="${esc(name)}" aria-haspopup="dialog" aria-controls="engine-mission-dialog" ${disabled}><span class="engine-mission-current"><span>Mission</span><strong>${esc(name)}</strong></span></button>`;
}
function submitMissionDialog(event) {
    if (event.target.id === "engine-welcome-brief-form") {
      event.preventDefault();
      reviewFirstLaunchBrief(event.target).catch(missionError);
      return;
    }
    if (event.target.id === "engine-welcome-key-form") {
      event.preventDefault();
      if (missionMutation) return;
      const input = event.target.elements.key;
      if (!input) {
        showOnboardingStep("review");
        return;
      }
      const key = input.value.trim();
      if (!key || /\s/.test(key)) {
        missionError(new Error("Enter an API key without spaces, or skip for now."));
        return;
      }
      input.value = "";
      saveFirstLaunchKey(event.target, key).catch(missionError);
      return;
    }
    if (event.target.id === "engine-company-tag-form") {
      event.preventDefault();
      changeCompanyTag(event.target.elements.tag.value, "add").catch(missionError);
      return;
    }
    if(event.target.id === "engine-mission-edit-form") {
      event.preventDefault();editMissionTitle(event.target).catch(missionError);return;
    }
    if(event.target.id === "engine-mission-compile-form") {
      event.preventDefault();compileExistingMission(event.target).catch(missionError);return;
    }
    if(event.target.id !== "engine-mission-form")return;
    event.preventDefault();
    createMission(event.target, event.submitter).catch(missionError);
}
function bindMissionDialog() {
  const dialog=document.querySelector("#engine-mission-dialog");
  if(!dialog)return;
  dialog.onsubmit=submitMissionDialog;
  dialog.onclick=event=>{
    event.stopPropagation?.();
    const node=event.target.closest("[data-engine]");
    if(node)handleClick(node).catch(missionError);
  };
}
function renderMissions() {
  callbacks.onboardingChanged?.(needsSalesMission());
  const node=document.querySelector("#engine-missions");
  if(!node)return;
  node.innerHTML=missionControls();
  bindMissionDialog();
  node.onclick=event=>{
    const button=event.target.closest('[data-engine="mission-manage"]');
    if(button && !button.disabled)handleClick(button).catch(missionError);
  };
}

function missionDialogShell(title, body, footer, {formId="", attributes=""} = {}) {
  const tag=formId ? "form" : "div";
  return `<${tag} ${formId ? `id="${formId}"` : ""} class="engine-mission-dialog-content" ${attributes}><header class="engine-mission-dialog-head"><div><span class="engine-mission-eyebrow">Prospecting missions</span><h2 id="engine-mission-dialog-title" tabindex="-1" ${body.includes(" autofocus") ? "" : "autofocus"}>${esc(title)}</h2></div><button type="button" class="engine-mission-close" data-engine="mission-cancel" aria-label="Close mission dialog">×</button></header><div class="engine-mission-dialog-body">${body}<p id="engine-mission-error" class="engine-mission-dialog-error" role="alert" hidden></p></div><footer class="engine-mission-dialog-actions">${footer}</footer></${tag}>`;
}
function missionManager() {
  const disabled=missionMutation ? "disabled" : "";
  const rows=missions.filter(m=>!m.archived).map(m=>{
    const active=m.id===missionId;
    const compileDisabled=missionMutation || ["running","uncertain"].includes(m.compilation?.status) ? "disabled" : "";
    const compile=m.id!=="beings" && m.compilation?.status!=="ready" ? button(m.compilation?.can_retry ? "Retry with AI" : "Compile with AI", "mission-compile", `data-id="${esc(m.id)}" ${compileDisabled}`) : "";
    return `<div class="engine-mission-row ${active ? "is-active" : ""}"><button type="button" class="engine-mission-choice" data-engine="mission-select" data-id="${esc(m.id)}" aria-pressed="${active}" ${disabled}><strong>${esc(m.name)}</strong><span>${active ? "Selected mission" : "Switch to this mission"}</span></button><div class="engine-mission-row-actions">${compile}${button("Edit title", "mission-edit", `data-id="${esc(m.id)}" aria-label="Edit ${esc(m.name)} title" ${disabled}`)}<button type="button" class="engine-mission-remove" data-engine="mission-remove" data-id="${esc(m.id)}" aria-label="Remove ${esc(m.name)}" title="Remove mission" ${disabled}>×</button></div></div>`;
  }).join("");
  const archived=missions.filter(m=>m.archived);
  const removed=archived.length ? `<section class="engine-mission-archived"><h3>Removed missions</h3><div class="engine-mission-list">${archived.map(m=>`<div class="engine-mission-row engine-mission-restore-row"><div class="engine-mission-choice"><strong>${esc(m.name)}</strong><span>Removed mission</span></div><div class="engine-mission-row-actions">${button("Restore", "mission-restore", `data-id="${esc(m.id)}" aria-label="Restore ${esc(m.name)}" ${disabled}`)}<button type="button" class="engine-mission-remove engine-mission-delete" data-engine="mission-delete" data-id="${esc(m.id)}" aria-label="Permanently delete ${esc(m.name)}" title="${m.id==="beings" ? "Default mission cannot be deleted" : "Permanently delete mission"}" ${m.id==="beings" ? "disabled" : disabled}>×</button></div></div>`).join("")}</div></section>` : "";
  const all=`<button type="button" class="engine-mission-inventory ${missionId==="all" ? "is-active" : ""}" data-engine="mission-select" data-id="all" aria-pressed="${missionId==="all"}" ${disabled}><strong>All companies</strong><span>Ranked by shared Sales priority</span></button>`;
  return missionDialogShell("Missions", `<div class="engine-mission-list">${rows}${needsSalesMission() ? "" : all}</div>${removed}`, `${button("Done", "mission-cancel", disabled)}<button type="button" class="button primary" data-engine="mission-new" ${disabled}>+ New mission</button>`);
}
function missionTarget(mission) {
  return `<section class="engine-mission-target"><span class="engine-mission-eyebrow">Mission</span><h3>${esc(mission.name)}</h3><p>${esc(mission.brief || "")}</p></section>`;
}
function openMissionDialog(type, id=missionId) {
  if(missionMutation)return;
  const dialog=document.querySelector("#engine-mission-dialog");
  if(!dialog)return;
  const mission=missions.find(m=>m.id===id);
  if(["edit","compile","delete"].includes(type) && !mission)return;
  if(type==="delete" && (!mission.archived || id==="beings"))return;
  missionDialogView={type,id};
  const cancel=button("Cancel", "mission-cancel");
  if(type==="manage")dialog.innerHTML=missionManager();
  if(type==="new")dialog.innerHTML=missionDialogShell("New mission", `<label class="engine-mission-field"><span>Mission title</span><input name="name" maxlength="80" required autofocus placeholder="e.g. Corporate photography"></label><label class="engine-mission-field"><span>What companies are you looking for?</span><textarea name="brief" minlength="5" maxlength="2000" required placeholder="Example: Find buyers of corporate event photography.&#10;Must organize corporate events. Prefer companies in NYC.&#10;Prefer recurring events shown in public calendars or dated recaps.&#10;Exclude photography vendors and venues that only rent space."></textarea></label>${missionSetupOptions()}${compilerOptions()}`, `${cancel}<button type="submit" class="button primary">Save & compile mission</button>`, {formId:"engine-mission-form"});
  if(type==="edit")dialog.innerHTML=missionDialogShell("Edit mission title", `${missionTarget(mission)}<label class="engine-mission-field"><span>Mission title</span><input name="name" value="${esc(mission.name)}" maxlength="80" required autofocus></label>`, `${cancel}<button type="submit" class="button primary">Save title</button>`, {formId:"engine-mission-edit-form",attributes:`data-mission-id="${esc(id)}"`});
  if(type==="delete")dialog.innerHTML=missionDialogShell("Delete mission?", `${missionTarget(mission)}<p class="engine-mission-delete-note">Permanently delete this mission, its rankings and mission history. This cannot be undone. Shared companies, evidence and contacts will stay.</p>`, `${button("Cancel", "mission-manage")}<button type="button" class="button engine-mission-delete-confirm" data-engine="mission-delete-confirm" data-id="${esc(id)}">Delete mission</button>`);
  if(type==="compile") {
    const compilation=mission.compilation;
    const retry=compilation?.can_retry;
    const label=retry ? "Retry mission with AI" : "Compile mission";
    const pin=retry ? ` data-retry-key="${esc(compilation.cache_key)}" data-retry-model="${esc(compilation.model)}"` : "";
    dialog.innerHTML=missionDialogShell(retry ? "Retry mission" : "Compile mission", `${missionTarget(mission)}${compilerOptions(compilation?.model || defaultCompilerModel)}`, `${cancel}<button type="submit" class="button primary">${label}</button>`, {formId:"engine-mission-compile-form",attributes:`data-mission-id="${esc(id)}"${pin}`});
  }
  dialog.oncancel=event=>{if(missionMutation)event.preventDefault();else missionDialogView=null;};
  if(!dialog.open)dialog.showModal();
  const focus=dialog.querySelector('[autofocus]') || dialog.querySelector('select') || dialog.querySelector('#engine-mission-dialog-title');
  focus?.focus();
}
function closeMissionDialog() {
  if(missionMutation)return;
  document.querySelector("#engine-mission-dialog")?.close();missionDialogView=null;
}
function missionError(error) {
  const node=missionDialogView ? document.querySelector("#engine-mission-error")
    : document.querySelector("#engine-first-launch-error");
  if(node){node.textContent=error.message;node.hidden=false;}else message(error.message);
}
function missionFormBusy(form, busy) {
  form.setAttribute?.("aria-busy",String(busy));
  form.querySelectorAll?.("input,textarea,select,button").forEach(node=>{node.disabled=busy;});
  const error=document.querySelector("#engine-mission-error");if(error)error.hidden=true;
  const welcomeError=form.querySelector?.("#engine-first-launch-error");
  if(welcomeError && busy)welcomeError.hidden=true;
  renderMissions();
}
export async function loadSalesMissions() {
  const value = await api("/missions");
  if (!Array.isArray(value.items)) return;
  missions = value.items;
  if(value.compiler) {compilerModels=value.compiler.models;defaultCompilerModel=value.compiler.default_model;}
  if (needsSalesMission()) await changeMission(null);
  else if (missionId !== "all" && !missions.some(m => m.id === missionId && !m.archived)) {
    await changeMission(userMissionRequired ? value.default_mission_id : "all");
  }
  renderMissions();
  renderList();
}
async function changeMission(id) {
  if (id === missionId) return;
  if (needsSalesMission() ? id !== null : id !== "all" && !missions.some(m => m.id === id && !m.archived)) return;
  missionId = id;
  rankingPace = null;
  try {
    if (id === null) localStorage.removeItem("beings-mission");
    else localStorage.setItem("beings-mission", id);
  } catch { /* Session only. */ }
  invalidateSalesWorkbench(); resetSelection(); listOffset=0;
  state.overview=null; pipelineData=null; currentListPath=""; listSignature="";
  renderMissions(); renderPacket();
  if(active)await loadSalesWorkbench();
}
async function hideMission(id, archived) {
  if (missionMutation) return;
  missionMutation=true;
  try {
    const response=await fetch(`/api/sales-engine/missions/${encodeURIComponent(id)}`, {
      method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({archived})});
    const value=await response.json();
    if(!response.ok)throw Error(value.detail || "Could not update mission");
    missions=value.items;
    if(archived && (missionId===id || needsSalesMission())) await changeMission(missions.find(m=>!m.archived)?.id || (userMissionRequired ? null : "all"));
    renderMissions();
  } finally {
    missionMutation=false;renderMissions();
    if(missionDialogView?.type==="manage")document.querySelector("#engine-mission-dialog").innerHTML=missionManager();
  }
}
async function deleteMission(id) {
  if(missionMutation || id==="beings" || !missions.some(m=>m.id===id && m.archived))return;
  const dialog=document.querySelector("#engine-mission-dialog");
  missionMutation=true;missionFormBusy(dialog,true);
  try {
    const response=await fetch(`/api/sales-engine/missions/${encodeURIComponent(id)}`, {method:"DELETE"});
    const value=await response.json();
    if(!response.ok)throw Error(value.detail || "Could not delete mission");
    missions=value.items;
    if(missionId===id)await changeMission(missions.find(m=>!m.archived)?.id || (userMissionRequired ? null : "all"));
    missionMutation=false;openMissionDialog("manage");
  } finally {missionMutation=false;missionFormBusy(dialog,false);renderMissions();}
}
async function editMissionTitle(form) {
  if(missionMutation)return;
  missionMutation=true;missionFormBusy(form,true);
  try {
    const response=await fetch(`/api/sales-engine/missions/${encodeURIComponent(form.dataset.missionId)}`, {
      method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:form.elements.name.value})});
    const value=await response.json();
    if(!response.ok)throw Error(value.detail || "Could not save mission title");
    missions=value.items;document.querySelector("#engine-mission-dialog")?.close();missionDialogView=null;
    renderList();
    message("Mission title saved.");
  } finally {missionMutation=false;missionFormBusy(form,false);}
}
async function createMission(form, submitter) {
  if (missionMutation) return;
  missionMutation=true;
  missionFormBusy(form,true);
  const submit = submitter || form.querySelector('button[type="submit"]');
  const deferAI = submit?.value === "defer";
  const submitLabel=submit?.textContent;
  if(submit){submit.disabled=true;submit.textContent=deferAI ? "Saving draft…" : "Compiling mission…";}
  try {
    const {name, brief, compiler_model, target_role, ranking_focus} = form.elements;
    const created = await api("/missions",{
      name:name.value, brief:brief.value, compile_with_ai:true, compiler_model:compiler_model.value,
      ...(deferAI ? {defer_ai:true} : {}),
      ...(target_role && ranking_focus ? {setup:{target_role:target_role.value, ranking_focus:ranking_focus.value}} : {}),
    });
    missions.push(created); document.querySelector("#engine-mission-dialog")?.close();missionDialogView=null;
    if (form.closest?.(".engine-welcome")) {
      onboardingStep = "storage";
      onboardingDraft = {name:"My mission", brief:"", model:""};
    }
    form.reset(); await changeMission(created.id);
    message(deferAI ? "Draft saved. Add an OpenAI API key in Settings to build your mission." : compilationMessage(created));
  } finally {missionMutation=false;missionFormBusy(form,false);if(submit){submit.disabled=false;submit.textContent=submitLabel;}}
}
async function compileExistingMission(form) {
  if(missionMutation)return;
  missionMutation=true;
  missionFormBusy(form,true);
  const id=form.dataset.missionId;
  const submit=form.querySelector('button[type="submit"]');
  const label=submit?.textContent || "Compile mission";
  if(submit){submit.disabled=true;submit.textContent="Compiling mission…";}
  try {
    const body={compiler_model:form.elements.compiler_model.value};
    if(form.dataset.retryKey && body.compiler_model===form.dataset.retryModel)body.retry_cache_key=form.dataset.retryKey;
    const value=await api(`/missions/${encodeURIComponent(id)}/compile`,body);
    missions=missions.map(m=>m.id===id ? value : m);
    document.querySelector("#engine-mission-dialog")?.close();missionDialogView=null;
    responseCache.clear();renderMissions();
    if(missionId===id)await loadSalesWorkbench({invalidate:true});
    const notice=compilationMessage(value);
    message(notice ? `${value.name || "Mission"}: ${notice}` : "");
  } finally {missionMutation=false;missionFormBusy(form,false);if(submit){submit.disabled=false;submit.textContent=label;}}
}
let companySort = "ranked";
function missionPath(path) {
  if (missionId === "beings") return path;
  return `${path}${path.includes("?") ? "&" : "?"}mission_id=${encodeURIComponent(missionId)}`;
}
let pipelineData = null;
let listOffset = 0;
const pageSizes = [10,25,50,100,250];
let pageSize = 50;
try { const saved = Number(localStorage.getItem("beings-company-page-size")); if (pageSizes.includes(saved)) pageSize = saved; } catch { /* Storage may be unavailable. */ }
async function changePageSize(value) {
  const size = Number(value);
  if (!pageSizes.includes(size) || size === pageSize) return;
  pageSize = size; listOffset = 0;
  try { localStorage.setItem("beings-company-page-size", String(size)); } catch { /* Keep the session preference. */ }
  await loadSalesWorkbench();
}
async function changePage(value) {
  const data = state.tab === "pipeline" ? pipelineData : state.overview;
  if (data?.loading) return;
  const page = Number(value);
  if (!Number.isInteger(page)) return;
  const pages = Math.max(1, Math.ceil((data?.total ?? data?.items?.length ?? 0) / pageSize));
  const offset = (Math.max(1, Math.min(page, pages)) - 1) * pageSize;
  if (offset === listOffset) return;
  listOffset = offset;
  await loadSalesWorkbench();
}
const pipelineStage = "";
let callbacks = {};
let selectedKey = "";
let active = false;
let rendered = false;
let listSignature = "";
let currentListPath = "";
let rankingPace = null;
let expiryTimer = null;
const rankingRefreshInterval = 15_000;
const responseCache = new Map();
const controllers = new Map();
const CACHE_LIMIT = 48;
export function configureSalesWorkbench(value) { callbacks = value; }
export function salesSearch() { return state.search; }
export function browseCompanyKey(event) {
  if (!active || event.defaultPrevented || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter"].includes(event.key)
      || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
  const target = event.target;
  if (target?.isContentEditable || target?.closest?.("input, textarea, select, [role='textbox'], [role='slider'], [role='dialog'], dialog")) return;
  if (event.key === "Enter") {
    if (event.repeat || target?.closest?.("a, button:not(.engine-company), [role='button']")) return;
    if (!selectedKey || !state.packet?.company_key) return;
    event.preventDefault();
    window.open(`https://${state.packet.company_key}`, "_blank", "noopener,noreferrer");
    return;
  }
  const saved = state.tab === "pipeline";
  const data = saved ? pipelineData : state.overview;
  if (data?.loading) return;
  const items = data?.items || [];
  const savedOf = item => saved || !item.candidate_id;
  const idOf = item => savedOf(item) ? item.account_id : item.candidate_id;
  const index = items.findIndex(item => selectionPath(idOf(item), savedOf(item)) === selectedKey);
  const next = index < 0 ? 0 : index + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1);
  if (next < 0 || next >= items.length) return;
  event.preventDefault();
  const id = idOf(items[next]);
  void loadPacket(id, savedOf(items[next]));
  document.querySelector(`.engine-company[data-id="${id}"]`)?.scrollIntoView?.({block:"nearest"});
  if (target?.closest?.(".engine-company")) document.querySelector(`.engine-company[data-id="${id}"]`)?.focus?.({preventScroll:true});
}

export function salesMode() { return state.tab === "pipeline" ? "pipeline" : "prospects"; }
function isLoading(kind) {
  const controller = controllers.get(kind);
  return Boolean(active && controller && !controller.finished && !controller.signal.aborted);
}
function loadingIcon(kind, label, visibleLabel = false) {
  return `<span class="loading-indicator" data-engine-loading="${kind}" role="status" ${isLoading(kind) ? "" : "hidden"}><span class="loading-spinner" aria-hidden="true"></span><span${visibleLabel ? "" : ' class="sr-only"'}>${esc(label)}</span></span>`;
}
function syncContactLookupButton() {
  const stage = emailLookupStages.get(state.packet?.candidate_id);
  const loading = isLoading("summary");
  for (const control of document.querySelectorAll("#engine-contacts [data-lookup-label]")) {
    control.disabled = Boolean(stage || loading);
    if (stage?.action === control.dataset.engine || loading) control.setAttribute("aria-busy", "true");
    else control.removeAttribute("aria-busy");
    control.textContent = stage?.action === control.dataset.engine ? stage.label
      : loading ? "Loading contacts…" : control.dataset.lookupLabel;
  }
}
function syncLoadingIndicators() {
  syncContactLookupButton();
  syncHeaderStatus();
  for (const node of document.querySelectorAll("[data-engine-loading]")) node.hidden = !isLoading(node.dataset.engineLoading);
  for (const [kind, selector] of [["list", "#engine-list"], ["summary", "#engine-lead"]]) {
    document.querySelector(selector)?.setAttribute("aria-busy", String(isLoading(kind)));
  }
}
function abortRequest(kind) { controllers.get(kind)?.abort(); controllers.delete(kind); syncLoadingIndicators(); }
function startRequest(kind, path) {
  const current = controllers.get(kind);
  if (current && current.path === path && !current.signal.aborted && !current.finished) return current;
  abortRequest(kind);
  const controller = new AbortController(); controller.path = path;
  controllers.set(kind, controller); syncLoadingIndicators(); return controller;
}
function cancelled(error) { return error?.name === "AbortError"; }
function remember(cache, key, value, limit) {
  cache.delete(key); cache.set(key, value);
  while (cache.size > limit) cache.delete(cache.keys().next().value);
}
function expired(value) { return value?.valid_until && Date.parse(value.valid_until) <= Date.now(); }
function retainsPublishedScore() { return missionId !== "beings" && missionId !== "all"; }
function safeSummary(value) {
  return expired(value) ? {...value, sales_priority:retainsPublishedScore() ? value.sales_priority : {score:null}, contact_recommendations:{people:[],company_contact:null}, summary_loading:true} : value;
}
function resetSelection() {
  ++requestNumber; abortRequest("summary");
  selectedKey = ""; state.packet = null;
  companyWebsite?.select(null);
}
export function deactivateSalesWorkbench() {
  rankingPace = null;
  active = false; ++requestNumber; ++overviewNumber;
  scoreDialog?.close();
  for (const kind of [...controllers.keys()]) abortRequest(kind);
  syncLoadingIndicators();
  clearTimeout(expiryTimer);
}
export function invalidateSalesWorkbench() {
  ++requestNumber; ++overviewNumber;
  for (const kind of [...controllers.keys()]) abortRequest(kind);
  clearTimeout(expiryTimer);
  responseCache.clear();
  if (state.packet) state.packet = {...state.packet, valid_until:stamp()};
  if (state.overview) state.overview = {...state.overview, cache_expired:true};
  if (pipelineData) pipelineData = {...pipelineData, cache_expired:true};
}
export function setSalesMode(mode) {
  const tab = mode === "pipeline" ? "pipeline" : "queue";
  if (state.tab !== tab) { listOffset = 0; resetSelection(); ++overviewNumber; abortRequest("list"); }
  state.tab = tab;
}
export function captureSalesLocation() {
  return {
    missionId, tab:state.tab, search:state.search, selectedTag, companySort,
    listOffset, pageSize, selectedKey,
    listScroll:document.querySelector(".engine-company-list")?.scrollTop || 0,
    workspaceScroll:document.querySelector(".engine-company-workspace")?.scrollTop || 0,
  };
}
export function restoreSalesLocation(saved) {
  if (!saved || saved.missionId !== missionId) return false;
  state.tab = saved.tab === "pipeline" ? "pipeline" : "queue";
  state.search = typeof saved.search === "string" ? saved.search : "";
  selectedTag = typeof saved.selectedTag === "string" ? saved.selectedTag : "";
  companySort = saved.companySort === "newest" ? "newest" : "ranked";
  if (pageSizes.includes(saved.pageSize)) pageSize = saved.pageSize;
  listOffset = Number.isSafeInteger(saved.listOffset) && saved.listOffset >= 0 ? saved.listOffset : 0;
  selectedKey = /^\/(companies|pipeline)\/[1-9]\d*$/.test(saved.selectedKey || "") ? saved.selectedKey : "";
  return true;
}
export function restoreSalesScroll(saved) {
  for (const [selector, key] of [[".engine-company-list", "listScroll"], [".engine-company-workspace", "workspaceScroll"]]) {
    const node = document.querySelector(selector);
    if (node && Number.isFinite(saved?.[key]) && saved[key] >= 0) node.scrollTop = saved[key];
  }
}

export async function searchSalesWorkbench(query) {
  state.search = query; listOffset = 0; resetSelection();
  await loadSalesWorkbench();
}
let requestNumber = 0;
let overviewNumber = 0;
async function api(path, body, method = "POST") {
  const missionChange = body !== undefined && (path === "/missions" || /^\/missions\/[^/]+\/compile$/.test(path));
  const unconfirmed = path === "/missions" ? "Mission save status unavailable." : "Mission build status unavailable.";
  let ref = missionChange ? globalThis.crypto?.randomUUID?.()?.replaceAll("-", "").slice(0, 12) : "";
  const headers = {"Content-Type": "application/json"};
  if (ref) headers["X-Mission-Request-ID"] = ref;
  let response;
  try {
    response = await fetch(path.startsWith("/api/") ? path : `/api/sales-engine${path}`, body === undefined ? {} : {method,headers,body:JSON.stringify(body)});
  } catch (error) {
    if (!missionChange) throw error;
    console.warn("Mission request transport failed", error.name, ref);
    throw new Error(unconfirmed);
  }
  if (missionChange) ref = response.headers?.get("X-Mission-Request-ID") || ref;
  let value;
  try {
    value = await response.json();
  } catch {
    if (missionChange) console.warn("Mission response was not JSON", response.status, ref);
    throw new Error(missionChange ? unconfirmed
      : `The server returned an unexpected response (HTTP ${response.status}).`);
  }
  if (!response.ok) {
    if (missionChange) {
      console.warn("Mission request failed", response.status, ref);
      if (response.status >= 500) throw new Error(unconfirmed);
    }
    throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail || value));
  }
  return value;
}
async function cachedRead(path, controller) {
  if (controller.readPromise) return controller.readPromise;
  controller.readPromise = fetchCached(path, controller).finally(() => { controller.finished = true; syncLoadingIndicators(); });
  return controller.readPromise;
}
async function fetchCached(path, controller) {
  const previous = responseCache.get(path);
  const response = await fetch(`/api/sales-engine${path}`, {
    signal:controller.signal, headers:previous?.etag ? {"If-None-Match":previous.etag} : {},
  });
  if (controller.signal.aborted) throw new DOMException("Request superseded", "AbortError");
  if (response.status === 304 && previous) { previous.receivedAt = Date.now(); return previous.value; }
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail || value));
  if (controller.signal.aborted) throw new DOMException("Request superseded", "AbortError");
  remember(responseCache, path, {value, etag:response.headers?.get("ETag"), receivedAt:Date.now()}, CACHE_LIMIT);
  return value;
}
let connectionStatus = null;
let pageStatus = "";
document.addEventListener?.("beings-page-status", event => {
  pageStatus = event.detail?.text || "";
  syncHeaderStatus();
});
function isLoadingMessage(text) { return /^Loading(?:…|\.\.\.)$/.test(text); }
function isConnectionInterrupted(text) { return text === "The VM connection was interrupted."; }
function syncHeaderStatus() {
  const banner = document.querySelector("#company-loading-banner");
  if (!banner) return;
  const list = state.tab === "pipeline" ? pipelineData : state.overview;
  const message = active ? state.message : "";
  const loading = isLoadingMessage(message) || (list?.loading && isLoading("list"))
    || ((!state.packet || state.packet.summary_loading) && isLoading("summary"));
  const notice = isConnectionInterrupted(message) ? message : connectionStatus?.title;
  const text = notice || (!active ? pageStatus : message && !isLoadingMessage(message) ? message : loading ? "Loading…" : "");
  const label = banner.querySelector(".header-status-label");
  if (label && label.textContent !== text) label.textContent = text;
  const spinner = banner.querySelector(".loading-spinner");
  if (spinner) spinner.hidden = !(text === "Loading…" || text === "Reconnecting…" || text === "Connecting…");
  banner.title = text === connectionStatus?.title ? connectionStatus.detail || text : text;
  banner.hidden = !text;
}
document.addEventListener?.("beings-connection-status", event => {
  connectionStatus = event.detail;
  if (!connectionStatus && (isConnectionInterrupted(state.message) || isLoadingMessage(state.message))) {
    state.message = "";
  }
  syncHeaderStatus();
});
function message(text) {
  state.message = text;
  syncHeaderStatus();
}
function firstLaunch() {
  const storage = vmDashboard ? "Your virtual machine" : "This computer";
  const steps = Object.keys(onboardingSteps);
  const current = steps.indexOf(onboardingStep);
  const progress = `<ol class="engine-welcome-progress" aria-label="Setup progress">${steps.map((step, index) =>
    `<li ${index === current ? 'aria-current="step"' : ""} class="${index < current ? "complete" : ""}">${onboardingSteps[step]}</li>`).join("")}</ol>`;
  const error = `<p id="engine-first-launch-error" class="engine-mission-dialog-error" role="alert" hidden></p>`;
  const back = button("Back", "mission-onboarding-back");
  const actions = (label, value = "") => `<div class="engine-welcome-actions">${back}
    <button class="button primary engine-welcome-continue" type="submit" value="${value}">${label}</button>
  </div>`;

  let body;
  if (onboardingStep === "storage") {
    const choices = [
      ["local", "This computer", "Keep your leads on the computer running the app."],
      ["vm", "My virtual machine", "Keep your leads on a server you control."],
    ];
    body = `
      <h1 id="engine-welcome-title" tabindex="-1">Where should your leads live?</h1>
      <p>Set up Beings Growth to find companies, rank them against your goals, and discover business contacts.</p>
      <div class="engine-welcome-choices">${choices.map(([mode, label, detail]) => {
        const currentStorage = (mode === "vm") === vmDashboard;
        return `<button type="button" class="button engine-welcome-choice${currentStorage ? " current" : ""}" data-engine="mission-onboarding-${mode}">
          <span class="engine-welcome-choice-title"><strong>${label}</strong>${currentStorage ? '<span class="badge moss">Current storage</span>' : ""}</span>
          <span>${detail}</span>
        </button>`;
      }).join("")}</div>
      ${onboardingStorageHelp ? `<p class="engine-welcome-note" role="status">${onboardingStorageHelp}</p>` : ""}
      <small>Your leads stay in the storage you choose. Beings receives no lead data.</small>`;
  } else if (onboardingStep === "brief") {
    body = `
      <h1 id="engine-welcome-title" tabindex="-1">Who are you looking for?</h1>
      <p>Describe the companies you want to find. Include the industry, market, or signs that make them a good fit.</p>
      <form id="engine-welcome-brief-form" class="engine-welcome-form">
        <label class="engine-mission-field"><span class="sr-only">Target companies</span>
          <textarea name="brief" minlength="5" maxlength="2000" required
            placeholder="Example: Mid-sized US retailers with online stores that may need SEO help. Prefer companies expanding to new markets.">${esc(onboardingDraft.brief)}</textarea></label>
        ${error}${actions("Continue")}
      </form>`;
  } else if (onboardingStep === "key") {
    body = `
      <h1 id="engine-welcome-title" tabindex="-1">Connect OpenAI</h1>
      <p>OpenAI turns your target into ranking criteria and unlocks cited web search in Smart Scan.</p>
      <form id="engine-welcome-key-form" class="engine-welcome-form" method="post" autocomplete="off">
        ${onboardingKeyConfigured ? '<p class="engine-welcome-note"><strong>OpenAI API key saved</strong></p>' : `
          <label class="engine-mission-field"><span>OpenAI API key</span>
            <input type="password" name="key" placeholder="sk-…" maxlength="500" required
              autocomplete="new-password" spellcheck="false" aria-describedby="engine-welcome-key-note"></label>
          <small id="engine-welcome-key-note">The app saves your key on the selected computer or VM. <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">Get an OpenAI API key ↗</a></small>`}
        ${error}
        <div class="engine-welcome-actions">${back}
          ${onboardingKeyConfigured ? "" : button("Skip for now", "mission-onboarding-skip", "", "button engine-welcome-skip")}
          <button class="button primary engine-welcome-continue" type="submit">${onboardingKeyConfigured ? "Continue" : "Save key &amp; continue"}</button>
        </div>
      </form>`;
  } else {
    body = `
      <h1 id="engine-welcome-title" tabindex="-1">Review your mission</h1>
      <div class="engine-welcome-summary"><small>Target companies · ${storage}</small><p>${esc(onboardingDraft.brief)}</p></div>
      <form id="engine-mission-form" class="engine-welcome-form">
        <input type="hidden" name="brief" value="${esc(onboardingDraft.brief)}">
        <label class="engine-mission-field"><span>Mission name</span>
          <input name="name" value="${esc(onboardingDraft.name)}" maxlength="80" required></label>
        ${compilerOptions(onboardingDraft.model || defaultCompilerModel, true)}
        <small>${onboardingKeyConfigured
          ? "Build mission sends your brief to OpenAI once, within the cap above."
          : "Saving a draft lets you browse All companies. When you add an OpenAI key in Settings, the draft will compile within the cap above."}</small>
        ${error}${onboardingKeyConfigured ? actions("Build mission", "compile") : actions("Save mission draft", "defer")}
      </form>`;
  }
  return `<section class="panel engine-welcome" aria-labelledby="engine-welcome-title">
    <header class="engine-welcome-header"><span class="engine-mission-eyebrow">Getting started</span><span>Step ${current + 1} of ${steps.length}</span></header>
    ${progress}${body}
  </section>`;
}

function showOnboardingStep(step) {
  onboardingStep = step;
  onboardingStorageHelp = "";
  renderList();
  if (active) document.querySelector("#engine-welcome-title")?.focus();
}

async function reviewFirstLaunchBrief(form) {
  if (missionMutation) return;
  const brief = form.elements.brief.value.trim();
  if (brief.length < 5) {
    missionError(new Error("Enter at least five characters to describe your target."));
    return;
  }
  onboardingDraft.brief = brief;
  missionMutation = true;
  missionFormBusy(form, true);
  try {
    const result = await api("/api/settings/openai-key");
    onboardingKeyConfigured = result.configured === true;
  } catch {
    // The user can still enter a key or save a draft.
  } finally {
    missionMutation = false;
    missionFormBusy(form, false);
  }
  showOnboardingStep("key");
}

async function saveFirstLaunchKey(form, key) {
  const submit = form.querySelector('[type="submit"]');
  const label = submit.textContent;
  missionMutation = true;
  missionFormBusy(form, true);
  submit.textContent = "Saving key…";
  try {
    const result = await api("/api/settings/openai-key", {key}, "PUT");
    onboardingKeyConfigured = result.configured === true;
  } catch {
    onboardingKeyConfigured = false;
  } finally {
    missionMutation = false;
    missionFormBusy(form, false);
    submit.textContent = label;
  }
  if (!onboardingKeyConfigured) {
    form.elements.key.focus();
    missionError(new Error("Couldn’t save the OpenAI key. Try again or skip for now."));
    return;
  }
  void callbacks.scanCredentialsChanged?.();
  showOnboardingStep("review");
}

export function updateFirstLaunchKey(configured) {
  if (!needsSalesMission() || onboardingKeyConfigured === configured) return;
  onboardingKeyConfigured = configured;
  renderList();
}

function chooseFirstLaunchStorage(mode) {
  if ((mode === "vm") === vmDashboard) {
    showOnboardingStep("brief");
    return;
  }
  const bridge = window.webkit?.messageHandlers?.storageSettings;
  if (bridge) {
    bridge.postMessage(mode === "vm" ? "open-virtual-machine" : "open-local");
    onboardingStorageHelp = "Save your choice in Storage Mode, then quit and reopen to continue.";
  } else {
    onboardingStorageHelp = mode === "vm"
      ? `Follow the <a href="${VM_SETUP_GUIDE}" target="_blank" rel="noopener noreferrer">VM setup guide</a>, then open the dashboard through your SSH tunnel.`
      : "Start Beings Growth on this computer and open its local dashboard.";
  }
  renderList();
  document.querySelector(".engine-welcome-note")?.querySelector("a")?.focus();
}

function queue() {
  if (needsSalesMission()) return firstLaunch();
  return companyList(state.overview || {items:[]}, false);
}
function updatingRankingsLabel() {
  const title = currentMissionTitle();
  return title ? `Updating rankings to target ${title}` : "Updating rankings";
}
function observeRankingProgress(data, now = Date.now()) {
  const coverage = data.coverage;
  const count = coverage?.reviewed_locally, total = coverage?.inventory_total;
  const paused = ["waiting_desktop", "smart_scan", "mission_setup", "retrying", "other_mission", "preparing"];
  if (missionId === "beings" || missionId === "all" || !coverage?.more_may_exist
      || data.scoring_available === false || paused.includes(data.ranking_worker_status)
      || !Number.isSafeInteger(count) || !Number.isSafeInteger(total) || count < 0 || count >= total) {
    rankingPace = null;
    return;
  }
  const last = rankingPace?.samples.at(-1);
  if (rankingPace?.mission !== missionId || !last || count < last.count
      || total !== rankingPace.total || now - last.time > 60000 || now < last.time
      || now - (rankingPace.lastAdvance ?? last.time) > 45000) {
    rankingPace = {mission:missionId, total, samples:[{count, time:now}], lastAdvance:null};
    return;
  }
  if (count > last.count) rankingPace.lastAdvance = now;
  if (now === last.time) last.count = count;
  else rankingPace.samples.push({count, time:now});
  while (rankingPace.samples.length > 2 && rankingPace.samples[0].time < now - 180000) {
    rankingPace.samples.shift();
  }
}
function rankingEstimate(total, processed) {
  if (rankingPace?.mission !== missionId || rankingPace.total !== total) return "";
  const [first] = rankingPace.samples, last = rankingPace.samples.at(-1);
  if (last.count !== processed || rankingPace.lastAdvance === null
      || last.time - rankingPace.lastAdvance > 45000 || last.time - first.time < 30000
      || last.count - first.count < 100) return "";
  // Repeated polls or a single large batch cannot establish a reliable pace.
  const samples = rankingPace.samples;
  const advances = samples.slice(1).filter((sample, i) => sample.count > samples[i].count);
  if (advances.length < 6) return "";
  const rate = (last.count - first.count) / (last.time - first.time);
  let previous = first;
  for (let quarter = 1; quarter <= 4; quarter++) {
    const boundary = first.time + (last.time - first.time) * quarter / 4;
    const next = samples.find(sample => sample.time >= boundary);
    if (!next || next.time <= previous.time) return "";
    const periodRate = (next.count - previous.count) / (next.time - previous.time);
    if (periodRate < rate * 0.75 || periodRate > rate * 1.25) return "";
    previous = next;
  }
  const minutes = Math.ceil((total - processed) * (last.time - first.time)
    / ((last.count - first.count) * 60000));
  if (minutes >= 1440) return `About ${Math.ceil(minutes / 1440)} days left`;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60), remainder = minutes % 60;
    return `About ${hours} hr${remainder ? ` ${remainder} min` : ""} left`;
  }
  return `About ${minutes} min left`;
}
function rankingProgress(data) {
  if (missionId === "beings" || missionId === "all") return "";
  if (data?.scoring_available === false) {
    const compilation = data.compilation;
    const running = compilation?.status === "running";
    const needsKey = ["configure_openai_key", "provider_authentication_failed"].includes(compilation?.error_code);
    const title = running ? "Preparing mission" : needsKey ? "Waiting for an OpenAI key" : "Mission setup blocked";
    const text = compilationErrorText(compilation) || (running ? "AI setup is running." : "Complete mission setup to start ranking.");
    const action = needsKey ? '<button class="button primary" type="button" data-view="settings">Open Settings</button>'
      : compilation?.can_retry ? button("Retry with AI", "mission-compile", `data-id="${esc(missionId)}" ${missionMutation ? "disabled" : ""}`) : button("Missions", "mission-manage");
    return `<section class="engine-ranking-progress" role="status" aria-labelledby="engine-ranking-progress-title"><div class="engine-ranking-progress-head"><span id="engine-ranking-progress-title">${title}</span>${action}</div><p class="engine-ranking-progress-counts">${esc(text)}</p></section>`;
  }
  const coverage = data?.coverage;
  if (!coverage?.more_may_exist) return "";
  const total = coverage.inventory_total, processed = coverage.reviewed_locally;
  if (!Number.isSafeInteger(total) || total <= 0 || !Number.isSafeInteger(processed) || processed < 0 || processed >= total) return "";
  const phases = {
    waiting: "Waiting for the next ranking batch",
    waiting_desktop: "Waiting for the desktop ranking worker",
    preparing: "Preparing ranking data",
    ranking_views: "Refreshing saved ranking views",
    selecting: "Selecting companies to rank",
    scoring_missions: "Ranking missions together",
    other_mission: data.ranking_worker_mission_name
      ? `Waiting for ${data.ranking_worker_mission_name} to finish ranking before proceeding`
      : "Waiting while another mission ranks",
    smart_scan: "Re-ranking paused until Smart Scan completes",
    mission_setup: "Waiting for mission setup",
    retrying: "Waiting to retry ranking",
  };
  const title = phases[data.ranking_worker_status] || updatingRankingsLabel();
  const estimate = rankingEstimate(total, processed);
  const percent = Math.floor(processed * 10_000 / total) / 100;
  const percentage = percent ? `${percent.toLocaleString()}%` : "&lt;0.01%";
  return `<section class="engine-ranking-progress" aria-labelledby="engine-ranking-progress-title"><div class="engine-ranking-progress-head"><span id="engine-ranking-progress-title">${esc(title)}</span>${processed ? `<strong>${percentage}</strong>` : ""}</div><progress max="${total}" value="${processed}" aria-labelledby="engine-ranking-progress-title" aria-describedby="engine-ranking-progress-counts"></progress><div id="engine-ranking-progress-counts" class="engine-ranking-progress-counts"><span>${processed.toLocaleString()} of ${total.toLocaleString()} companies ranked</span><span>${(total - processed).toLocaleString()} remaining${estimate ? ` · ${estimate}` : ""}</span></div></section>`;
}
function companyList(data, saved) {
  const items = data.items || [];
  const total = data.total ?? items.length;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const page = Math.floor(listOffset / pageSize) + 1;
  const pending = Boolean(data.loading);
  const range = items.length ? `${(listOffset+1).toLocaleString()}–${(listOffset+items.length).toLocaleString()} of ${total.toLocaleString()}` : `0 of ${total.toLocaleString()}`;
  const paging = `<div class="engine-pagination" role="group" aria-label="Company pagination">
    <label class="engine-page-size" for="engine-page-size"><span class="sr-only">Companies per page</span>
      <select id="engine-page-size">${pageSizes.map(size => `<option value="${size}" ${size === pageSize ? "selected" : ""}>${size}</option>`).join("")}</select>
      <span aria-hidden="true">/ page</span>
    </label>
    <div class="engine-page-actions">
      ${button("Import", "import", 'aria-label="Import companies"')}
      ${button("Export", "export", 'aria-label="Export companies as CSV"')}
    </div>
    <div class="engine-page-navigation">
      ${button("‹", "previous", `${!pending && listOffset > 0 ? "" : "disabled"} aria-label="Previous page"`)}
      <label class="engine-page-number" for="engine-page-number"><span class="sr-only">Page</span><input id="engine-page-number" type="number" min="1" max="${pages}" value="${pending ? 1 : page}" ${pending || !total ? "disabled" : ""}><span> / ${pending ? "…" : pages.toLocaleString()}</span></label>
      ${button("›", "next", `${!pending && listOffset + items.length < total ? "" : "disabled"} aria-label="Next page"`)}
    </div>
  </div>`;
  const scope = `<label class="engine-scope"><span class="sr-only">Company list</span><select id="engine-scope"><option value="queue" ${!saved && companySort === "ranked" ? "selected" : ""}>Ranked companies</option><option value="newest" ${!saved && companySort === "newest" ? "selected" : ""}>Newly discovered</option><option value="pipeline" ${saved ? "selected" : ""}>Saved companies</option></select></label>`;
  const rows = items.map(item => {
    const isSaved = saved || !item.candidate_id;
    const id = isSaved ? item.account_id : item.candidate_id;
    const isSelected = selectionPath(id, isSaved) === selectedKey;
    // The list deadline triggers revalidation; each row owns its score expiry.
    const score = !retainsPublishedScore() && (expired(item) || data.cache_expired) ? null : item.sales_priority?.score ?? item.priority;
    return `<button class="engine-company ${isSelected ? "selected" : ""}" data-engine="${isSaved ? "pipeline-company" : "company"}" data-id="${id}" data-website="${esc(`https://${item.company_key}`)}" aria-pressed="${isSelected}"><span><strong>${esc(item.company_name)}</strong><small>${esc(item.company_key)}</small></span><span class="engine-priority" aria-label="Sales priority ${score ?? "unscored"} out of 100">${score ?? "—"}</span></button>`;
  }).join("");
  const research = !["beings", "all"].includes(missionId) && data.scoring_available !== false
    ? button("Scan company websites · $0.00", "mission-research", `title="Scan up to 25 company websites due for research in this mission" ${missionResearchPending ? "disabled" : ""}`)
    : "";
  const scanMore = total < 50 && !pending && !data.load_error && data.scoring_available !== false
    ? '<div class="engine-scan-more"><button type="button" class="button primary" data-view-target="intelligence">Scan to Find More</button></div>'
    : "";
  const tagActions = selectedTag
    ? button("Re-score tag", "tag-rescore", `title="Re-score this entire tag from saved evidence; no provider charges" ${tagMutation ? "disabled" : ""}`)
      + button("Re-scan tag", "tag-rescan", `title="Fetch public websites again, then re-score; no paid providers" ${tagMutation ? "disabled" : ""}`)
    : "";
  const researchActions = research || tagActions
    ? `<div class="engine-list-research">${research}${tagActions}</div>` : "";
  return `<section class="panel engine-queue"><div class="panel-head engine-list-header">${scope}<label class="engine-scope"><span class="sr-only">Filter by tag</span><select id="engine-tag"><option value="">All tags</option>${[...new Set(["starred", ...companyTags.map(item => item.tag), ...(selectedTag ? [selectedTag] : [])])].sort().map(tag => `<option value="${esc(tag)}" ${tag === selectedTag ? "selected" : ""}>${esc(tagLabel(tag))}</option>`).join("")}</select></label><span class="engine-list-status">${pending ? loadingIcon("list", loadingCompanies) : `<span class="engine-page-range" aria-live="polite">${range}</span>`}</span></div>${rankingProgress(data)}<div class="engine-company-list">${rows || `<p class="engine-empty">${data.load_error ? "Could not load companies." : data.loading ? `${loadingCompanies}…` : data.scoring_available === false ? "Mission setup is incomplete." : !saved && data.coverage?.more_may_exist ? `${esc(updatingRankingsLabel())}…` : data.scope === "mission_matches" ? "No mission matches in saved evidence." : "No companies found."}</p>`}${scanMore}</div>${researchActions}${paging}</section>`;
}

const pendingPersonSaves = new Map();
let personSaveRevision = 0;
function pendingPersonKey(company, key) { return `${company}:${key}`; }
function savedPerson(person, packet = state.packet) {
  const pending = pendingPersonSaves.get(pendingPersonKey(packet?.company_key, person.prospect_key));
  if (pending) return pending;
  const rows = packet?.outbound_people || [];
  const exact = rows.find(row => row.person_key === person.prospect_key);
  if (exact) return exact;
  const matches = rows.filter(row => row.name.normalize("NFKC").toLowerCase() === person.name.normalize("NFKC").toLowerCase()
    && (!row.profile_url || !person.profile_url));
  return matches.length === 1 ? matches[0] : null;
}
const pendingAIContacts = new Set();
const pendingPhoneContacts = new Set();
const pendingContactFields = new Map();
const INDIVIDUAL_CONTACT_LOOKUPS = {
  "find-contact-email": {field:"email", route:"find-email", label:"email", estimate:"$0.03"},
  "find-contact-profile": {field:"profile_url", route:"find-profile", label:"profile", estimate:"$0.01"},
  "find-contact-profile-web": {field:"profile_url", route:"find-profile-web", label:"profile"},
};
const phoneRetryStates = new Map();
const exhaustedContactFields = new Map();
const profileWebAvailable = new Set();
const exhaustedLookupHelp = "All paths to find this information have been exhausted.";

function exhaustedLookupButton(label, action, person) {
  return `<span class="engine-lookup-exhausted" title="${exhaustedLookupHelp}">${button(label, action,
    `disabled data-person-key="${esc(person.prospect_key)}" aria-description="${exhaustedLookupHelp}"`)}</span>`;
}
const PHONE_PROFILE = /^https?:\/\/(www\.)?linkedin\.com\/in\/[A-Za-z0-9._%~-]+\/?(?:[?#].*)?$/i;
const COMPANY_PHONE_BATCH_LIMIT = 10;
// Catalog prices for LeadOcean, BlitzAPI and the conditional Tomba stage.
const COMPANY_PHONE_ESTIMATE_MICROUSD = COMPANY_PHONE_BATCH_LIMIT * (5000 + 495 + 44500);

function canFindPhone(person) {
  return Boolean(person.prospect_key && person.name && person.name !== person.email
    && person.name !== person.phone && !person.phone && !person.phone_lookup_blocked
    && PHONE_PROFILE.test(person.profile_url || ""));
}

function rememberPhoneLookup(result, packet, key) {
  if (result.contact) rememberProspect(result.contact, packet.company_key);
  if (!result.found && result.retry_used && result.complete !== false) {
    phoneRetryStates.set(key, "exhausted");
  } else if (result.retry_used) phoneRetryStates.set(key, "used");
  else if (result.retry_available) phoneRetryStates.set(key, "available");
}

const contactUsdFormatter = new Intl.NumberFormat("en-US", {style:"currency", currency:"USD"});

function contactCharge(microusd) {
  return contactUsdFormatter.format(microusd / 1_000_000);
}
const aiContactRetryKeys = new Map();
export function contactLookupActions(person, packet) {
  const key = pendingPersonKey(packet?.company_key, person.prospect_key);
  const pending = pendingAIContacts.has(key);
  const findingPhone = pendingPhoneContacts.has(key);
  const findingField = pendingContactFields.get(key);
  const companyLookup = emailLookupStages.get(packet?.candidate_id);
  const retry = phoneRetryStates.get(key);
  const phoneLabel = retry === "available" ? "Try again · $0.05" : "Find phone";
  const named = person.prospect_key && packet?.candidate_id
    && person.name !== person.email && person.name !== person.phone;
  const needsPhone = !person.phone && !person.phone_lookup_blocked;
  const label = aiContactRetryKeys.has(key) ? "Search again with AI" : "Prospect with AI";
  const find = named && (!person.email || needsPhone)
    ? button(pending ? "Searching web…" : label, "find-contact-ai", `data-person-key="${esc(person.prospect_key)}" aria-label="${label} for email and phone for ${esc(person.name)}, $0.20 maximum" ${pending ? 'disabled aria-busy="true"' : findingPhone || findingField || companyLookup?.action === "emails" ? "disabled" : ""}`) : "";
  const phoneDisabled = pending || findingField || companyLookup;
  const findPhone = named && canFindPhone(person) && retry !== "used"
    && packet?.email_lookup?.configured === true
    ? retry === "exhausted" ? exhaustedLookupButton("Find phone", "find-contact-phone", person)
      : button(findingPhone ? "Finding..." : phoneLabel, "find-contact-phone",
      `data-person-key="${esc(person.prospect_key)}" aria-label="${phoneLabel} for ${esc(person.name)}" ${findingPhone ? 'disabled aria-busy="true"' : phoneDisabled ? "disabled" : ""}`) : "";
  const fieldButton = action => {
    const {field, label, estimate} = INDIVIDUAL_CONTACT_LOOKUPS[action];
    const web = action === "find-contact-profile-web";
    const providerReady = web ? packet.email_lookup?.microlink_configured
      : packet.email_lookup?.configured;
    if (!named || !person.name.trim().includes(" ") || person[field]
        || person[`${field}_lookup_blocked`] || !providerReady) return "";
    if (exhaustedContactFields.get(key)?.has(action)) {
      return exhaustedLookupButton(`Find ${label}`, action, person);
    }
    const finding = findingField === action;
    const text = web ? "Search web · 1 request" : `Find ${label}`;
    const ceiling = web ? "1 Microlink subscription request" : `Up to ${estimate}`;
    return button(finding ? "Finding..." : text, action,
      `data-person-key="${esc(person.prospect_key)}" aria-label="${text} for ${esc(person.name)}, ${ceiling}" title="${ceiling}" ${finding ? 'disabled aria-busy="true"' : pending || findingPhone || findingField || companyLookup ? "disabled" : ""}`);
  };
  const profileAction = profileWebAvailable.has(key) && packet.email_lookup?.microlink_configured
    ? "find-contact-profile-web" : "find-contact-profile";
  return {phone:findPhone, ai:find,
    email:fieldButton("find-contact-email"), profile:fieldButton(profileAction)};
}
function contactActions(person) {
  const saved = savedPerson(person);
  const save = person.prospect_key && state.packet?.candidate_id
    ? button(saved ? "Saved" : "Save", "save-person",
      `data-person-key="${esc(person.prospect_key)}" aria-label="${saved ? "Saved" : "Save"} ${esc(person.name)}" ${saved?.pending ? 'aria-busy="true" title="Saving in background"' : ""} ${saved ? "disabled" : ""}`,
      "button primary") : "";
  return `<div class="engine-contact-actions">${save}</div>`;
}
function contactPeople(p) {
  const current = (p.contact_recommendations?.people || []).map(person => {
    const saved = savedPerson(person, p);
    return {...person, email:saved?.email_lookup_blocked ? null : person.email || saved?.email,
      phone:saved?.phone || person.phone,
      profile_url:saved?.profile_url_lookup_blocked ? null : person.profile_url || saved?.profile_url,
      ai_contact:saved?.ai_contact || {}, public_work_phone:saved?.public_work_phone,
      provider_contact:saved?.provider_contact,
      email_lookup_blocked:saved?.email_lookup_blocked,
      profile_url_lookup_blocked:saved?.profile_url_lookup_blocked,
      phone_lookup_blocked:saved?.phone_lookup_blocked};
  });
  const companyEmail = p.contact_recommendations?.company_contact?.email?.toLowerCase();
  const extra = (p.outbound_people || []).filter(row => (row.operator_supplied || row.public_work_phone || Object.keys(row.ai_contact || {}).length
    || Object.keys(row.provider_contact || {}).length)
    && !(row.email && row.name === row.email && row.email.toLowerCase() === companyEmail)
    && !current.some(person => row.person_key === person.prospect_key
      || (row.profile_url && row.profile_url === person.profile_url)
      || (row.email && row.email.toLowerCase() === person.email?.toLowerCase())))
    .map(row => ({...row, prospect_key:row.person_key}));
  return [...current, ...extra];
}
function exactProspectSource(value) {
  try {
    const url = new URL(value);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password) return "";
    const path = url.pathname.replace(/\/+$/, "");
    // Treg's people-search landing page identifies the provider, not the person's record.
    if (url.hostname.replace(/^www\./, "") === "treg.to" && path === "/people-search") return "";
    return path || url.search || url.hash ? value : "";
  } catch {
    return "";
  }
}
function prospectSources(person) {
  const source = !person.operator_supplied && exactProspectSource(person.source_url);
  const emailSource = person.email
    ? exactProspectSource(person.email_source_url) : "";
  const evidence = aiContactSources(person) + Object.entries(person.provider_contact || {})
    .filter(([field, item]) => item.value === person[field])
    .map(([field, item]) => `<p>${field === "email" ? "Work email" : "Profile"}: ${esc(item.provider)}</p>`)
    .join("");
  const profile = !source && person.source_kind === "licensed_professional_provider"
    ? exactProspectSource(person.profile_url) : "";
  return (source ? link(source, "View source") : "")
    + (emailSource && emailSource !== source ? `<p>Work email: ${link(emailSource, "View source")}</p>` : "")
    + evidence + (profile ? `<p>${link(profile, "View person’s profile")}</p>` : "")
    + (!person.operator_supplied && !source && !emailSource && !evidence
      ? '<p class="muted">Exact source page not recorded.</p>' : "");
}
function aiContactSources(person) {
  const publicPhone = person.public_work_phone;
  const provider = {leadocean: "LeadOcean", blitzapi: "BlitzAPI", tomba: "Tomba"}[publicPhone?.provider];
  const providerSourced = publicPhone?.source_kind === "licensed_professional_provider";
  const publicSource = publicPhone && publicPhone.value === person.phone
    ? providerSourced
      ? `<p>Phone source: ${esc(provider || "Prospecting provider")} · ${link(publicPhone.source_url, "Matched profile ↗")}</p>`
      : `<p>Work phone: ${link(publicPhone.source_url, "Company website ↗")}</p>`
    : "";
  return publicSource + ["email", "phone"].map(field => {
    const source = person.ai_contact?.[field];
    return source && source.value === person[field] ? `<p>${field === "email" ? "Work email" : "Work phone"}: ${link(source.source_url, "AI web search ↗")}${source.observed_at ? ` · ${date(source.observed_at)}` : ""}</p>` : "";
  }).join("");
}
const pendingEmailSaves = new Set();
function savedEmail(email, packet = state.packet) {
  return packet?.outbound_people?.some(row => row.email?.toLowerCase() === email.toLowerCase());
}
function rememberProspect(result, company) {
  ++personSaveRevision;
  if (state.packet?.company_key === company) {
    state.packet.outbound_people = [...(state.packet.outbound_people || []).filter(row => row.id !== result.id), result];
    state.packet.has_saved_prospects = true;
    renderPacket();
  }
  callbacks.prospectChanged?.();
}
async function saveCompanyEmail(node) {
  const packet = state.packet;
  const email = packet?.contact_recommendations?.company_contact?.email;
  if (!packet?.candidate_id || !email) return;
  const existing = packet.outbound_people?.find(row => row.email?.toLowerCase() === email.toLowerCase());
  if (existing) {await callbacks.editProspect?.(existing.id); return;}
  const key = `${packet.company_key}:${email.toLowerCase()}`;
  if (pendingEmailSaves.has(key)) return;
  pendingEmailSaves.add(key); renderPacket();
  try {
    const result = await api("/outbound/manual", {candidate_id:packet.candidate_id, email});
    rememberProspect(result, packet.company_key);
  } catch (error) {
    if (callbacks.notify) callbacks.notify(error.message, "error"); else message(error.message);
  } finally {
    pendingEmailSaves.delete(key);
    if (state.packet?.company_key === packet.company_key) renderPacket();
  }
}
export function openManualProspect(packet = state.packet, prospect = null, onSaved = null) {
  if (!packet?.candidate_id) return;
  const dialog = document.createElement("dialog");
  dialog.className = "modal compact engine-prospect-dialog";
  dialog.setAttribute("aria-labelledby", "engine-prospect-title");
  dialog.innerHTML = `<form class="modal-card">
    <header class="modal-head"><h2 id="engine-prospect-title">${prospect ? "Update prospect" : "Add prospect"} · ${esc(packet.company_name)}</h2>
      <button type="button" class="icon-button" data-prospect-close aria-label="Close">×</button></header>
    <div class="modal-body"><label class="field"><span>Name</span><input name="name" maxlength="200" autocomplete="name" autofocus></label>
      <label class="field"><span>Role <small>optional</small></span><input name="title" maxlength="200" placeholder="e.g. Head of Marketing"></label>
      <label class="field"><span>Email</span><input name="email" type="email" maxlength="254" autocomplete="email"></label>
      <label class="field"><span>Work phone <small>optional</small></span><input name="phone" type="tel" maxlength="40" autocomplete="tel" placeholder="+1 212 555 0142"></label>
      <label class="field"><span>LinkedIn / profile <small>optional</small></span><input name="profile_url" maxlength="2048" placeholder="https://linkedin.com/in/…"></label>
      <p class="muted">Enter a name, email, or work phone.</p><p data-prospect-message role="status"></p></div>
    <footer class="modal-actions"><button type="button" class="button" data-prospect-close>Cancel</button>
      <button type="submit" class="button primary">${prospect ? "Update prospect" : "Save prospect"}</button></footer></form>`;
  const form = dialog.querySelector("form");
  const status = dialog.querySelector("[data-prospect-message]");
  const save = dialog.querySelector('[type="submit"]');
  const fields = ["name", "title", "email", "phone", "profile_url"];
  const original = Object.fromEntries(fields.map(key => [key, prospect?.[key] || ""]));
  if (prospect?.phone && original.name === prospect.phone) original.name = "";
  fields.forEach(key => {form.elements[key].value = original[key];});
  let busy = false;
  const close = () => {
    if (busy) return;
    if (fields.some(key => form.elements[key].value !== original[key]) && !window.confirm("Discard unsaved prospect changes?")) return;
    dialog.close();
  };
  dialog.querySelectorAll("[data-prospect-close]").forEach(button => button.addEventListener("click", close));
  dialog.addEventListener("cancel", event => {event.preventDefault(); close();});
  dialog.addEventListener("close", () => dialog.remove(), {once:true});
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    const data = Object.fromEntries(fields.map(key => [key, form.elements[key].value.trim()]));
    if (!data.name && !data.email && !data.phone) {status.textContent = "Enter a name, email, or work phone."; return;}
    busy = true; save.disabled = true;
    fields.forEach(key => {form.elements[key].readOnly = true;});
    status.textContent = "Saving…";
    try {
      const result = prospect
        ? await api(`/outbound/${prospect.id}/details`, data, "PUT")
        : await api("/outbound/manual", {candidate_id:packet.candidate_id, ...data});
      rememberProspect(result, packet.company_key);
      dialog.close();
      await onSaved?.(result);
      if (callbacks.notify) callbacks.notify(prospect ? "Prospect updated" : "Prospect saved");
    } catch (error) {status.textContent = error.message;}
    finally {
      busy = false; save.disabled = false;
      fields.forEach(key => {form.elements[key].readOnly = false;});
    }
  });
  document.body.append(dialog); dialog.showModal();
}

function scanDate(label, stamp) {
  const checked = new Date(stamp);
  if (!stamp || !Number.isFinite(checked.getTime())) return "";
  return `<small class="muted">${label} <time datetime="${esc(stamp)}" title="${esc(checked.toLocaleString())}">${esc(checked.toLocaleDateString())}</time></small>`;
}

function contactTableRow(person) {
  const lookup = contactLookupActions(person, state.packet);
  const email = person.email
    ? `<button type="button" class="text-button engine-email" data-copy-email="${esc(person.email)}" aria-label="Copy ${esc(person.email)}">${esc(person.email)}</button>`
    : lookup.email || '<span class="muted">—</span>';
  const phone = person.phone && /^\+?[0-9]{7,15}$/.test(person.phone)
    ? `<a href="tel:${esc(person.phone)}" aria-label="Call ${esc(person.name)}">${esc(person.phone.replace(/^(?:\+1)?(\d{3})(\d{3})(\d{4})$/, "($1) $2-$3"))}</a>`
    : person.phone ? esc(person.phone) : lookup.phone || '<span class="muted">—</span>';
  return `<tr class="engine-contact">
    <td><div class="engine-person-info"><strong>${esc(person.name)}</strong>
      <span>${esc(person.title || "")}</span>
</div></td>
    <td>${person.profile_url ? link(person.profile_url, /^https?:\/\/(?:[a-z0-9-]+\.)?linkedin\.com\//i.test(person.profile_url) ? "LinkedIn ↗" : "Profile ↗") : lookup.profile || '<span class="muted">—</span>'}</td>
    <td><div class="engine-contact-email">${email}</div></td>
    <td class="engine-contact-phone">${phone}</td>
    <td>${contactActions(person)}</td>
  </tr>`;
}
function contactsTable(people, loading, companyRow = "") {
  if (!people.length && !companyRow) return `<p class="muted engine-contact-empty">${loading
    ? "Contacts loading..." : "No named contacts yet."}</p>`;
  return `<div class="engine-contacts-table-wrap" tabindex="0" role="region" aria-label="Company contacts">
    <table class="engine-contacts-table"><colgroup><col><col><col><col><col></colgroup>
      <thead><tr><th scope="col">Name</th><th scope="col">Profile</th><th scope="col">Email</th><th scope="col">Phone</th><th scope="col">Actions</th></tr></thead>
      <tbody>${companyRow}${people.map(contactTableRow).join("")}</tbody>
    </table></div>`;
}
function companyContactName(packet) {
  const name = String(packet.company_name || "").trim();
  const domain = String(packet.company_key || "");
  const compact = value => value.toLowerCase().replace(/[^a-z0-9]/g, "");
  const stem = domain.replace(/^www\./i, "").split(".")[0];
  // A generic page headline is not a verified company name.
  const headline = /\b(?:agency|services|solutions|marketing|creative|leading|award.winning)\b/i.test(name)
    && name.split(/\s+/).length >= 4 && !compact(name).includes(compact(stem));
  return name && !headline ? name : domain;
}
function contactPanel(p) {
  const contacts = p.contact_recommendations || {people:[], company_contact:null};
  const people = contactPeople(p);
  const fallback = contacts.company_contact;
  const companyPhone = /^\+?[0-9]{7,15}$/.test(fallback?.phone || "") ? fallback.phone : "";
  const website = `https://${p.company_key}`;
  const contactRoute = fallback?.url && fallback.url.replace(/\/$/, "") !== website;
  const emailMaximum = p.email_lookup_estimate?.people_checked_without_candidates
    ? 0 : p.email_lookup_estimate?.maximum_microusd ?? 53040;
  const estimatedSpend = Math.ceil((emailMaximum + COMPANY_PHONE_ESTIMATE_MICROUSD) / 10000) / 100;
  const lookupStage = emailLookupStages.get(p.candidate_id);
  const treg = p.email_lookup?.configured === true
    && (!p.email_lookup_estimate?.people_checked_without_candidates || people.some(canFindPhone));
  const microlink = p.email_lookup?.microlink_configured === true;
  const lookupLabel = treg ? `Find all ~$${estimatedSpend.toFixed(2)}` : "Find all";
  const lookupAttributes = `data-lookup-label="${esc(lookupLabel)}"`;
  const canSource = microlink || (treg && p.email_lookup_estimate?.needs_people_scan !== false);
  const sourceLabel = "Source contacts";
  const sourceButton = p.candidate_id && !p.summary_loading && canSource
    ? button(lookupStage?.action === "source-contacts" ? lookupStage.label : sourceLabel, "source-contacts",
      `data-lookup-label="${sourceLabel}"${lookupStage ? " disabled" : ""}${lookupStage?.action === "source-contacts" ? ' aria-busy="true"' : ""}`)
    : "";
  const emailControl = `<div class="engine-email-control">${p.candidate_id && !p.summary_loading ? button("Input prospect", "add-prospect") : ""}${sourceButton}${p.candidate_id && !p.summary_loading && (treg || microlink) ? button(lookupStage?.action === "emails" ? lookupStage.label : lookupLabel, "emails", lookupAttributes + (lookupStage ? " disabled" : "") + (lookupStage?.action === "emails" ? ' aria-busy="true"' : ""), "button primary") : ""}</div>`;
  const phoneLabel = companyPhone.replace(/^(?:\+1)?(\d{3})(\d{3})(\d{4})$/, "($1) $2-$3");
  const emailSaving = fallback?.email && pendingEmailSaves.has(`${p.company_key}:${fallback.email.toLowerCase()}`);
  const companyRow = fallback && (fallback.email || contactRoute || companyPhone)
    ? `<tr class="engine-contact engine-company-contact-row">
      <td><div class="engine-person-info"><strong>${esc(companyContactName(p))}</strong><span>Company contact</span></div></td>
      <td>${contactRoute ? link(fallback.url, "Visit ↗") : '<span class="muted">—</span>'}</td>
      <td><div class="engine-contact-email">${fallback.email ? `<button type="button" class="text-button engine-email" data-copy-email="${esc(fallback.email)}" aria-label="Copy ${esc(fallback.email)}">${esc(fallback.email)}</button>` : '<span class="muted">—</span>'}</div></td>
      <td class="engine-contact-phone">${companyPhone ? `<a href="tel:${esc(companyPhone)}" aria-label="Call company at ${esc(phoneLabel)}">${esc(phoneLabel)}</a>` : '<span class="muted">—</span>'}</td>
      <td><div class="engine-contact-actions">${fallback.email && p.candidate_id ? button(emailSaving ? "Saving…" : savedEmail(fallback.email, p) ? "Update" : "Save", "save-company-email", emailSaving ? 'disabled aria-busy="true"' : "", "button primary") : ""}</div></td>
    </tr>` : "";
  return `<section class="engine-contacts" id="engine-contacts"><div class="panel-head"><h3>Contacts</h3><div class="engine-contact-header-end">${p.summary_loading ? loadingIcon("summary", "Loading contacts") : ""}${emailControl}</div></div>${contactsTable(people, p.summary_loading, companyRow)}</section>`;
}

function scoreBreakdown(p) {
  const breakdown = p.score_breakdown;
  const score = p.sales_priority?.score;
  const format = value => Number.isFinite(value) ? Number(value.toFixed(1)).toString() : "—";
  const dimensions = (breakdown?.dimensions || []).map(part => `<div class="engine-score-part"><span>${esc(part.label)}</span><strong>${format(part.value)}<small>${Number.isFinite(part.value) ? "/100" : ""}</small>${Number.isFinite(part.points) ? `<small> · ${format(part.points)} pts</small>` : ""}</strong></div>`).join("");
  const adjustments = (breakdown?.adjustments || []).filter(part => part.label !== "Evidence score").map(part => `<div class="engine-score-part"><span>${esc(part.label)}</span><strong>${!part.base && part.value > 0 ? "+" : ""}${format(part.value)}</strong></div>`).join("");
  const available = Number.isFinite(score) && breakdown;
  const details = available
    ? [dimensions, adjustments].filter(Boolean).map(rows => `<div class="engine-score-parts">${rows}</div>`).join("")
    : `<p class="muted">${p.summary_loading ? "Loading score breakdown…" : "No scored evidence yet."}</p>`;
  return `<div class="engine-score-overview">
    <span>Sales priority</span>
    <div><strong>${format(score)}</strong>${Number.isFinite(score) ? "<small>/100</small>" : ""}</div>
  </div>
  ${details}`;
}

function openScoreBreakdown(summary) {
  if (!summary || scoreDialog?.open) return;
  const dialog = document.createElement("dialog");
  dialog.className = "modal engine-score-breakdown";
  dialog.dataset.companyKey = summary.company_key;
  dialog.setAttribute("aria-labelledby", "engine-score-breakdown-title engine-score-breakdown-company");
  dialog.innerHTML = `<header class="modal-head">
    <div>
      <h2 id="engine-score-breakdown-title" tabindex="-1" autofocus>Score breakdown</h2>
      <p id="engine-score-breakdown-company">${esc(summary.company_name)}</p>
    </div>
    <form method="dialog"><button type="submit" class="icon-button" aria-label="Close">×</button></form>
  </header><div class="modal-body">${scoreBreakdown(summary)}</div>`;
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (scoreDialog !== dialog) return;
    scoreDialog = null;
    if (active && state.packet?.company_key === summary.company_key) {
      document.querySelector('[data-engine="score-breakdown"]')?.focus({preventScroll:true});
    }
  }, {once:true});
  document.body.append(dialog);
  scoreDialog = dialog;
  dialog.showModal();
}

function companyHeading(p) {
  const score = p.sales_priority?.score;
  const websiteChecked = scanDate("checked", p.scan_dates?.website_checked_at);
  return `<header class="panel engine-lead-heading">
    <button type="button" class="engine-score" data-engine="score-breakdown" aria-haspopup="dialog"
      aria-label="Sales priority ${score ?? "unscored"} out of 100. Show score breakdown">
      <span>Sales priority</span>
      <span class="engine-score-value"><strong>${score ?? "—"}</strong><small>/100</small></span>
    </button>
    <div class="engine-company-identity"><div class="engine-company-title"><h2>${esc(p.company_name)}</h2>${companyStar(p)}</div>
      <div class="engine-website-meta">${link(`https://${p.company_key}`, `${p.company_key} ↗`)} ${websiteChecked}</div>
    </div>
    ${button("Contact", "jump-to-contacts", 'aria-controls="engine-contacts"', "button primary engine-contact-jump")}
  </header>`;
}
function lead(p) {
  const tags = companyTagControls(p);
  const lastRan = manualRunDate(p.email_lookup?.last_ran_at);
  const checks = [
    scanDate("Contact crawl", p.scan_dates?.contact_crawled_at),
    scanDate("People checked", p.scan_dates?.people_checked_at),
    lastRan ? `<small class="muted">Last ran ${esc(lastRan)}</small>` : "",
  ].filter(Boolean);
  const dates = checks.map((check, index) =>
    `<span>${index ? '<span aria-hidden="true">&#8226;</span>' : ""}${check}</span>`).join("");
  return `<section class="panel engine-card engine-lead">${contactPanel(p)}
    ${tags || dates ? `<div class="engine-company-controls">${tags}${dates ? `<div class="engine-scan-dates">${dates}</div>` : ""}</div>` : ""}
  </section>`;
}
function companyStar(p) {
  if (!p.candidate_id) return "";
  const starred = (p.tags || []).includes("starred");
  return `<button type="button" class="engine-company-star" data-engine="company-star" aria-pressed="${starred}" aria-label="${starred ? "Unstar company" : "Star company"}" title="${starred ? "Unstar company" : "Star company"}" ${p.summary_loading || companyTagSaves.has(p.candidate_id) ? "disabled" : ""}><span aria-hidden="true">${starred ? "★" : "☆"}</span></button>`;
}
function companyTagOptions(p, query = "") {
  const value = query.trim().toLowerCase();
  const assigned = new Set(p.tags || []);
  const available = companyTags.map(item => item.tag).filter(tag => tag !== "starred" && !assigned.has(tag));
  const matches = available.filter(tag => tag.toLowerCase().includes(value)).sort();
  return matches.map(tag => `<button type="button" class="engine-tag-option" data-engine="company-tag-add" data-tag="${esc(tag)}"><span>${esc(tag)}</span><span aria-hidden="true">+</span></button>`).join("")
    + (value && !assigned.has(value) && !available.includes(value) ? `<button type="submit" class="engine-tag-option engine-tag-create"><span>Create “${esc(query.trim())}”</span><span aria-hidden="true">↵</span></button>` : "")
    + (!matches.length && (!value || assigned.has(value)) ? `<p class="engine-tag-empty">${assigned.has(value) ? "Tag already added" : "Type to create a tag"}</p>` : "");
}
function companyTagControls(p) {
  if (!p.candidate_id || p.summary_loading) return "";
  const disabled = companyTagSaves.has(p.candidate_id) ? "disabled" : "";
  return `<div class="engine-company-tags" aria-label="Company tags">${(p.tags || []).filter(tag => tag !== "starred").map(tag => `<span class="engine-tag-chip"><span>${esc(tag)}</span><button type="button" data-engine="company-tag-remove" data-tag="${esc(tag)}" aria-label="Remove tag ${esc(tag)}" ${disabled}>×</button></span>`).join("")}<div class="engine-tag-picker"><button type="button" class="engine-tag-trigger" data-engine="company-tag-toggle" aria-expanded="false" aria-controls="engine-tag-popover" ${disabled}><span aria-hidden="true">+</span> Add tag</button><div id="engine-tag-popover" class="engine-tag-popover" hidden><form id="engine-company-tag-form"><label class="sr-only" for="engine-company-tag-input">Search or create a tag</label><input id="engine-company-tag-input" name="tag" placeholder="Search or create a tag…" maxlength="40" autocomplete="off" required ${disabled}><div id="engine-company-tag-options">${companyTagOptions(p)}</div></form></div></div></div>`;
}
function closeTagPicker() {
  const panel = document.querySelector("#engine-tag-popover");
  if (panel) panel.hidden = true;
  document.querySelector('[data-engine="company-tag-toggle"]')?.setAttribute("aria-expanded", "false");
}
async function changeCompanyTag(tag, action) {
  const cid = state.packet?.candidate_id;
  if (!cid || companyTagSaves.has(cid) || !tag.trim()) return;
  const normalized = tag.trim().toLowerCase().replace(/\s+/g, " ");
  const previousTags = [...(state.packet.tags || [])];
  const companyKey = state.packet.company_key;
  companyTagSaves.add(cid);
  // A summary already in flight must not overwrite immediate mutation feedback.
  ++requestNumber; abortRequest("summary");
  state.packet = {...state.packet, tags:action === "add"
    ? [...new Set([...previousTags, normalized])].sort()
    : previousTags.filter(value => value !== normalized)};
  renderPacket();
  let committed = false;
  try {
    const result = await api(`/companies/${cid}/tags`, {tag, action});
    committed = true;
    for (const [path, entry] of responseCache) {
      if (path.includes("/summary") && entry.value.company_key === companyKey
          || path.includes(`tag=${encodeURIComponent(normalized)}&`)) responseCache.delete(path);
    }
    if (state.packet?.candidate_id === cid) {
      state.packet = {...state.packet, tags:result.tags};
    }
  } catch (error) {
    if (state.packet?.candidate_id === cid) state.packet = {...state.packet, tags:previousTags};
    throw error;
  } finally {
    companyTagSaves.delete(cid);
    if (state.packet?.candidate_id === cid) renderPacket();
  }
  if (committed && active) {
    // Tag changes cannot affect the unfiltered ranking, scores or contacts.
    // Only an affected filtered list needs exact membership/totals revalidation.
    if (selectedTag === normalized) {
      abortRequest("list");
      void loadSalesWorkbench({refreshSummary:false}).catch(missionError);
    } else {
      const revision = overviewNumber;
      void api("/api/tags").then(data => {
        if (revision !== overviewNumber || !active) return;
        companyTags = data.items || []; renderList(true);
      }).catch(missionError);
    }
  }
}
function packet(summary) {
  return `<div id="engine-lead">${lead(summary)}</div>`;
}
function pipeline() { return companyList(pipelineData || {items:[],total:0}, true); }

function paneDivider(pane, label, orientation) {
  return `<div class="engine-pane-divider" role="separator" tabindex="0" aria-label="${label}" aria-controls="${pane}" aria-orientation="${orientation}"></div>`;
}

function render() {
  const root = document.querySelector("#view-sales");
  const summary = safeSummary(state.packet);
  const dialog = document.querySelector("#engine-mission-dialog") ? "" : `<dialog id="engine-mission-dialog" class="modal engine-mission-dialog" aria-labelledby="engine-mission-dialog-title"></dialog>`;
  root.innerHTML = `${dialog}<div class="engine-layout"><div id="engine-list">${state.tab === "pipeline" ? pipeline() : queue()}</div>${paneDivider("engine-list", "Resize company list", "vertical")}
    <div class="engine-company-workspace">
      <div id="engine-company-header">${summary ? companyHeading(summary) : ""}</div>
      <section id="engine-website" aria-label="Company website viewer"></section>
      ${paneDivider("engine-website", "Resize company website", "horizontal")}
      <div id="engine-packet">${summary ? packet(summary) : '<section class="panel engine-empty"></section>'}</div>
    </div></div>`;
  companyWebsite = createCompanyWebsite(document.querySelector("#engine-website"));
  companyWebsite.select(state.packet);
  root.classList?.toggle("engine-first-launch", needsSalesMission());
  renderMissions();
  rendered = true;
  root.onsubmit = submitMissionDialog;
  root.onclick = event => {
    if (!event.target.closest(".engine-tag-picker")) closeTagPicker();
    const node = event.target.closest("[data-engine]");
    if (node) handleClick(node).catch(error => { if (!cancelled(error)) missionError(error); });
  };
  root.oninput = event => {
    const {form, name, value} = event.target;
    if (form?.id === "engine-welcome-brief-form" && name === "brief") onboardingDraft.brief = value;
    if (form?.id === "engine-mission-form" && form.closest(".engine-welcome")) {
      if (name === "name") onboardingDraft.name = value;
      if (name === "compiler_model") onboardingDraft.model = value;
    }
    if (event.target.id === "engine-company-tag-input") {
      document.querySelector("#engine-company-tag-options").innerHTML = companyTagOptions(state.packet, event.target.value);
    }
  };
  root.onkeydown = event => {
    if (event.key === "Escape" && event.target.closest(".engine-tag-picker")) {
      event.preventDefault(); event.stopPropagation(); closeTagPicker();
      document.querySelector('[data-engine="company-tag-toggle"]')?.focus();
    }
  };
  root.ondblclick = event => {
    const row = event.target.closest(".engine-company");
    if (row?.dataset.website) window.open(row.dataset.website, "_blank", "noopener,noreferrer");
  };
  root.onchange = event => {
    if (event.target.id === "engine-page-size" || event.target.id === "engine-page-number") {
      const change = event.target.id === "engine-page-size" ? changePageSize : changePage;
      change(event.target.value).catch(error => { if (!cancelled(error)) message(error.message); });
      return;
    }
    if (event.target.id === "engine-tag") {
      selectedTag = event.target.value; listOffset = 0; resetSelection();
      loadSalesWorkbench().catch(error => { if (!cancelled(error)) message(error.message); });
      return;
    }
    if (event.target.id !== "engine-scope") return;
    const mode = event.target.value === "pipeline" ? "pipeline" : "sales";
    if (mode === "sales") {
      companySort = event.target.value === "newest" ? "newest" : "ranked";
      listOffset = 0;
      resetSelection();
    }
    const navigate = callbacks.navigate ? callbacks.navigate(mode) : (setSalesMode(mode), loadSalesWorkbench());
    Promise.resolve(navigate).catch(error => { if (!cancelled(error)) message(error.message); });
  };
}
function renderList(preservePosition = false) {
  const node = document.querySelector("#engine-list");
  const onboarding = needsSalesMission();
  document.querySelector("#view-sales")?.classList?.toggle("engine-first-launch", onboarding);
  const data = state.tab === "pipeline" ? pipelineData : state.overview;
  const signature = JSON.stringify(onboarding ? ["onboarding", onboardingStep, onboardingStorageHelp, onboardingKeyConfigured] :
    [state.tab, state.search, selectedTag, tagMutation, missionResearchPending, companyTags, missionId, currentMissionTitle(), companySort, listOffset, pageSize, pipelineStage, data,
    rankingEstimate(data?.coverage?.inventory_total, data?.coverage?.reviewed_locally),
    (data?.items || []).map(expired)]);
  if (node && listSignature !== signature) {
    const focus = document.activeElement?.id;
    const scrollTop = preservePosition ? node.querySelector(".engine-company-list")?.scrollTop : null;
    const focusedRow = preservePosition ? document.activeElement?.closest?.(".engine-company") : null;
    node.innerHTML = !onboarding && state.tab === "pipeline" ? pipeline() : queue();
    if (["engine-page-size", "engine-page-number"].includes(focus)) node.querySelector(`#${focus}`)?.focus?.();
    if (scrollTop != null) node.querySelector(".engine-company-list").scrollTop = scrollTop;
    if (focusedRow) node.querySelector(`.engine-company[data-engine="${focusedRow.dataset.engine}"][data-id="${focusedRow.dataset.id}"]`)?.focus?.({preventScroll:true});
    listSignature = signature;
  }
  for (const row of document.querySelectorAll(".engine-company")) {
    const selected = selectionPath(row.dataset.id, row.dataset.engine === "pipeline-company") === selectedKey;
    row.classList.toggle("selected", selected); row.setAttribute("aria-pressed", String(selected));
  }
  syncLoadingIndicators();
}
function renderPacket(replace = false) {
  companyWebsite?.select(state.packet);
  const summary = safeSummary(state.packet);
  if (scoreDialog?.open) {
    if (summary?.company_key === scoreDialog.dataset.companyKey) {
      scoreDialog.querySelector(".modal-body").innerHTML = scoreBreakdown(summary);
    } else {
      scoreDialog.close();
    }
  }
  const header = document.querySelector("#engine-company-header");
  if (header) header.innerHTML = summary ? companyHeading(summary) : "";
  const container = document.querySelector("#engine-packet");
  const current = document.querySelector("#engine-lead");
  if (!summary) {
    if (container) container.innerHTML = `<section class="panel engine-empty">${isLoading("summary") ? loadingIcon("summary", "Loading company…", true) : ""}</section>`;
    return;
  }
  if (replace || !current) { if (container) container.innerHTML = packet(summary); }
  else current.innerHTML = lead(summary);
  syncLoadingIndicators();
}
function armExpiry() {
  clearTimeout(expiryTimer);
  if (!active) return;
  const data = state.tab === "pipeline" ? pipelineData : state.overview;
  const deadlines = [state.packet, data, ...(data?.items || [])]
    .map(value => Date.parse(value?.valid_until)).filter(value => Number.isFinite(value) && value > Date.now());
  const ranking = retainsPublishedScore() && data?.coverage?.more_may_exist && data.scoring_available !== false;
  const rankingRefresh = ranking ? Date.now() + rankingRefreshInterval : Infinity;
  if (data?.compilation?.status === "running") deadlines.push(Date.now() + 5000);
  const background = rankingRefresh < Math.min(...deadlines);
  if (background) deadlines.push(rankingRefresh);
  if (!deadlines.length) return;
  expiryTimer = setTimeout(() => {
    if (background) {
      if (document.hidden || isLoading("list") || document.activeElement?.matches?.("#engine-list input, #engine-list select")) armExpiry();
      else void loadSalesWorkbench({background:true, refreshSummary:false});
      return;
    }
    renderList(); renderPacket();
    void loadSalesWorkbench();
  }, Math.min(2147483647, Math.max(1, Math.min(...deadlines) - Date.now() + 1)));
}
export async function openSalesCompany(candidateId, companyKey) {
  setSalesMode("prospects");
  state.search = companyKey;
  listOffset = 0;
  selectedKey = selectionPath(candidateId, false);
  state.packet = null;
  await callbacks.navigate?.("sales");
  await loadPacket(candidateId, false, {refresh:true});
}
function selectionPath(id, isPipeline) { return isPipeline ? `/pipeline/${Number(id)}` : `/companies/${Number(id)}`; }
async function loadPacket(id, isPipeline = false, {refresh = false} = {}) {
  const path = selectionPath(id, isPipeline), key = path;
  const summaryPath = missionPath(`${path}/summary`);
  const changedSelection = selectedKey !== key;
  const request = ++requestNumber;
  const controller = startRequest("summary", summaryPath);
  if (changedSelection) {
    selectedKey = key;
    const cached = responseCache.get(summaryPath);
    const items = isPipeline && state.tab === "pipeline" ? pipelineData?.items : state.overview?.items;
    const row = (items || []).find(item => (isPipeline ? item.account_id : item.candidate_id) === Number(id));
    state.packet = cached?.value || (row ? {
      candidate_id:row.candidate_id, company_key:row.company_key, company_name:row.company_name,
      account:isPipeline ? {id:Number(id)} : null,
      sales_priority:{score:!retainsPublishedScore() && expired(row) ? null : row.sales_priority?.score ?? row.priority},
      valid_until:row.valid_until, summary_loading:true,
    } : null);
    renderPacket(true); renderList();
  }
  try {
    const saveRevision = personSaveRevision;
    const value = await cachedRead(summaryPath, controller);
    const saved = value.candidate_id && (value.has_saved_prospects || value.contact_recommendations?.people?.length || value.contact_recommendations?.company_contact?.email || (state.packet?.company_key === value.company_key && state.packet.has_saved_prospects)) ? await api(`/outbound/company/${value.candidate_id}`).catch(() => ({items:[]})) : null;
    if (request !== requestNumber || selectedKey !== key) return;
    const previousRendered = state.packet;
    const rows = saved?.items || [];
    // A response started before a successful save must not erase that saved state.
    const committed = personSaveRevision !== saveRevision && state.packet?.company_key === value.company_key
      ? state.packet.outbound_people || [] : [];
    const savedRows = [...new Map([...rows,...committed].map(row => [row.id,row])).values()];
    state.packet = saved || committed.length ? {...value, has_saved_prospects:Boolean(savedRows.length), outbound_people:savedRows} : value;
    message("");
    if (previousRendered !== state.packet) renderPacket(!document.querySelector("#engine-lead"));
    renderList(); armExpiry();
  } catch (error) {
    if (!cancelled(error) && request === requestNumber && selectedKey === key) {
      message(error.message);
      if (!state.packet || state.packet.summary_loading) renderPacket();
    }
  }
}
async function runCompanyContactLookup(
  candidateId, maximum, updateStage, microlinkRequests = 0, sourceOnly = false,
) {
  const path = `/companies/${candidateId}/emails`;
  const result = await api(path, {
    confirmed_maximum_microusd:maximum,
    ...(sourceOnly ? {source_only:true} : {}),
    ...(microlinkRequests ? {confirmed_microlink_requests:microlinkRequests} : {}),
  });
  if (!result.waiting_for_people || sourceOnly) return result;
  updateStage("Waiting for people…");
  for (let attempt = 0; attempt < 60; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    if (!active || state.packet?.candidate_id !== candidateId) {
      return {...result, message:"The existing people scan continues. No email request was sent."};
    }
    const estimate = await api(`${path}/estimate`);
    if (estimate.people_scan_running) continue;
    if (estimate.needs_people_scan || estimate.people_checked_without_candidates) {
      return {...result, waiting_for_people:false, message:"People scan finished without eligible contacts. No email request was sent."};
    }
    updateStage("Finding contacts…");
    // Email-only ceiling prevents another paid people scan if contacts change meanwhile.
    try {
      const emails = await api(path, {
        confirmed_maximum_microusd:Math.min(maximum, estimate.email_maximum_microusd),
      });
      return {...result, ...emails, waiting_for_people:Boolean(emails.waiting_for_people)};
    } catch (error) {
      return {...result, waiting_for_people:false, actual_cost_usd:null,
        error:true, message:error.message};
    }
  }
  return {...result, message:"People scan is still running. No duplicate scan or email charge was submitted."};
}

async function runCompanyPhoneLookup(packet, summaryPath, lookupMission, updateStage) {
  const result = {found:0, failures:0, knownCost:0, chargeUnknown:false};
  updateStage("Finding phone numbers…");
  const summary = await api(summaryPath);
  if (summary.company_key !== packet.company_key) throw new Error("Company changed during lookup");
  const saved = await api(`/outbound/company/${packet.candidate_id}`);
  const people = contactPeople({...summary, outbound_people:saved.items || []})
    .filter(person => canFindPhone(person)
      && phoneRetryStates.get(pendingPersonKey(packet.company_key, person.prospect_key)) !== "exhausted")
    .slice(0, COMPANY_PHONE_BATCH_LIMIT);
  for (const person of people) {
    const key = pendingPersonKey(packet.company_key, person.prospect_key);
    if (pendingPhoneContacts.has(key) || pendingAIContacts.has(key) || pendingContactFields.has(key)) continue;
    pendingPhoneContacts.add(key);
    try {
      const path = `/companies/${packet.candidate_id}/contacts/find-phone`;
      const request = {person_key:person.prospect_key, mission_id:lookupMission};
      const record = lookup => {
        if (Number.isSafeInteger(lookup.cost_microusd)) result.knownCost += lookup.cost_microusd;
        else result.chargeUnknown = true;
        result.found += Number(Boolean(lookup.found));
        rememberPhoneLookup(lookup, packet, key);
      };
      const first = await api(path, request);
      record(first);
      let final = first;
      if (!first.found && first.retry_available && !first.retry_used && !first.retry_at) {
        final = await api(path, {...request, manual_retry:true});
        record(final);
      }
      if (final.complete === false) result.failures++;
    } catch {
      result.failures++;
      result.chargeUnknown = true;
    } finally {
      pendingPhoneContacts.delete(key);
    }
  }
  return result;
}

let contactScrollFrame;

function scrollToContacts() {
  cancelAnimationFrame(contactScrollFrame);
  const card = document.querySelector("#engine-contacts")?.closest(".engine-lead");
  if (!card) return;
  const pane = card.closest(".engine-company-workspace");
  const scroller = getComputedStyle(pane).overflowY === "auto"
    ? pane : card.closest("#view-sales");
  const selection = selectedKey;
  const start = scroller.scrollTop;
  const targetScrollTop = () => {
    const currentCard = document.querySelector("#engine-contacts")?.closest(".engine-lead");
    if (!currentCard || !scroller.contains(currentCard)) return null;
    const header = document.querySelector("#engine-company-header");
    const headerBounds = header?.getBoundingClientRect();
    const scrollTopEdge = scroller.getBoundingClientRect().top + scroller.clientTop;
    const pinnedBottom = headerBounds && headerBounds.top <= scrollTopEdge + 24
      ? headerBounds.bottom : 0;
    const top = Math.max(scrollTopEdge + (headerBounds?.height || 0), pinnedBottom) + 12;
    const end = scroller.scrollTop + currentCard.getBoundingClientRect().top - top;
    return Math.max(0, Math.min(end, scroller.scrollHeight - scroller.clientHeight));
  };
  const end = targetScrollTop();
  if (end === null) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || Math.abs(end - start) < 1) {
    scroller.scrollTop = end;
    // Re-measure after sticky positioning settles, including the mobile scroll inset.
    scroller.scrollTop = targetScrollTop() ?? end;
    return;
  }
  const startedAt = performance.now();
  let previous = start;
  function animate(now) {
    if (selectedKey !== selection || Math.abs(scroller.scrollTop - previous) > 1) return;
    const end = targetScrollTop();
    if (end === null) return;
    const progress = Math.min(1, (now - startedAt) / 400);
    const eased = progress * progress * (3 - 2 * progress);
    scroller.scrollTop = start + (end - start) * eased;
    previous = scroller.scrollTop;
    if (progress < 1) contactScrollFrame = requestAnimationFrame(animate);
  }
  contactScrollFrame = requestAnimationFrame(animate);
}

export async function runContactLookup(action, packet, person) {
  if (!packet?.candidate_id || !person?.prospect_key || !person.name
      || person.name === person.email || person.name === person.phone) return;
  const phone = action === "find-contact-phone";
  const fieldLookup = INDIVIDUAL_CONTACT_LOOKUPS[action];
  const ai = action === "find-contact-ai";
  if (ai && emailLookupStages.get(packet.candidate_id)?.action === "emails") return;
  if (fieldLookup && (person[fieldLookup.field] || person[`${fieldLookup.field}_lookup_blocked`]
      || emailLookupStages.has(packet.candidate_id))) return;
  if (phone && (!canFindPhone(person) || emailLookupStages.has(packet.candidate_id))) return;
  const key = pendingPersonKey(packet.company_key, person.prospect_key);
  const profileWeb = action === "find-contact-profile-web";
  if (profileWeb && (!profileWebAvailable.has(key) || !packet.email_lookup?.microlink_configured)) return;
  if (pendingAIContacts.has(key) || pendingPhoneContacts.has(key) || pendingContactFields.has(key)
      || exhaustedContactFields.get(key)?.has(action)
      || (phone && ["used", "exhausted"].includes(phoneRetryStates.get(key)))) return;
  const pending = phone ? pendingPhoneContacts : pendingAIContacts;
  const refreshActions = () => {
    if (state.packet?.company_key === packet.company_key) renderPacket();
    callbacks.contactLookupChanged?.();
  };
  const notify = (text, tone) => {
    if (callbacks.notify) callbacks.notify(text, tone);
    else if (state.packet?.company_key === packet.company_key) message(text);
  };
  if (fieldLookup) pendingContactFields.set(key, action);
  else pending.add(key);
  refreshActions();
  try {
    const result = await api(`/companies/${packet.candidate_id}/contacts/${fieldLookup?.route || (phone ? "find-phone" : "find-with-ai")}`, {
      person_key:person.prospect_key, mission_id:missionId,
      ...(ai ? {confirmed_maximum_microusd:200000}
        : phone && phoneRetryStates.get(key) === "available" ? {manual_retry:true} : {}),
      ...(ai && aiContactRetryKeys.has(key) ? {retry_cache_key:aiContactRetryKeys.get(key)} : {}),
    });
    if (fieldLookup) {
      if (result.contact) rememberProspect(result.contact, packet.company_key);
      if (result.web_search_available) profileWebAvailable.add(key);
      if (!result.found && (profileWeb || result.failed === false)) {
        const fields = exhaustedContactFields.get(key) || new Set();
        fields.add(action);
        exhaustedContactFields.set(key, fields);
      }
      const label = fieldLookup.label === "email" ? "Work email" : "Profile";
      const outcome = result.failed ? `${label} lookup could not finish`
        : result.found ? `${label} saved` : `No ${label.toLowerCase()} found`;
      const cost = Number.isSafeInteger(result.cost_microusd)
        ? ` · ${contactCharge(result.cost_microusd)} charged` : "";
      notify(outcome + cost, result.failed ? "error" : result.found ? "success" : "info");
    } else if (phone) {
      rememberPhoneLookup(result, packet, key);
      const outcome = result.found ? "Phone number saved"
        : result.retry_at ? "The phone provider didn’t finish. Try again in a few minutes."
        : result.complete === false ? "The phone search finished with limited results."
        : "No phone number found";
      const cost = Number.isFinite(result.cost_microusd)
        ? contactCharge(result.cost_microusd) : "";
      const details = [outcome, result.cached ? "Cached lookup" : "", cost ? `${cost} charged` : ""];
      notify(details.filter(Boolean).join(" · "), result.found ? "success" : "info");
    } else {
      if (result.contact) rememberProspect(result.contact, packet.company_key);
      if (result.cache_key) aiContactRetryKeys.set(key, result.cache_key);
      const confirmed = result.search?.completed_searches > 0;
      const source = result.cached ? "Cached web search (no new charge)" : `Web search complete (${result.search?.source_count ?? 0} sources)`;
      const notice = result.search?.research_complete === false
        ? `${result.cached ? "Cached incomplete research (no new charge)" : "Research ended early"} · use Search again to continue`
        : confirmed ? `${source} · ${result.found ? "contact details saved" : "no public work contact details found"}`
        : result.found ? "Contact details saved" : "Web search completion unconfirmed; no contact details saved";
      notify(notice, result.found ? "success" : result.search?.research_complete === false ? "error" : "info");
    }
  } catch (error) {
    notify(error.message, "error");
  } finally {
    if (fieldLookup) pendingContactFields.delete(key);
    else pending.delete(key);
    refreshActions();
  }
}

async function handleClick(node) {
  const action = node.dataset.engine;
  if (action === "jump-to-contacts") {
    scrollToContacts();
    return;
  }
  if (action === "score-breakdown") {
    openScoreBreakdown(safeSummary(state.packet));
    return;
  }
  if(action.startsWith("mission-") && missionMutation)return;
  if (action === "mission-onboarding-skip") {
    showOnboardingStep("review");
    return;
  }
  if (action === "mission-onboarding-local" || action === "mission-onboarding-vm") {
    chooseFirstLaunchStorage(action.endsWith("-vm") ? "vm" : "local");
    return;
  }
  if (action === "mission-onboarding-back") {
    const steps = Object.keys(onboardingSteps);
    showOnboardingStep(steps[steps.indexOf(onboardingStep) - 1]);
    return;
  }
  if (action === "find-contact-ai" || action === "find-contact-phone"
      || INDIVIDUAL_CONTACT_LOOKUPS[action]) {
    const packet = state.packet;
    const person = packet && contactPeople(packet).find(p => p.prospect_key === node.dataset.personKey);
    await runContactLookup(action, packet, person);
    return;
  }
  if (action === "add-prospect") {openManualProspect(); return;}
  if (action === "save-company-email") {await saveCompanyEmail(node); return;}
  if (action === "save-person") {
    const packet = state.packet;
    const person = packet?.contact_recommendations?.people?.find(p => p.prospect_key === node.dataset.personKey);
    if (!packet?.candidate_id || !person || savedPerson(person)) return;
    const key = pendingPersonKey(packet.company_key, person.prospect_key);
    const pending = {...person, pending:true, status:"saved", company_key:packet.company_key};
    pendingPersonSaves.set(key, pending);
    node.disabled = true;
    node.textContent = "Saved";
    node.setAttribute?.("aria-busy", "true");
    renderPacket();
    try {
      const result = await api("/outbound", {candidate_id:packet.candidate_id, person_key:person.prospect_key, mission_id:missionId});
      ++personSaveRevision;
      if (state.packet?.company_key === packet.company_key) {
        state.packet.outbound_people = [...(state.packet.outbound_people || []).filter(r => r.id !== result.id), result];
      }
      pendingPersonSaves.delete(key);
      if (state.packet?.company_key === packet.company_key) renderPacket();
    } catch (error) {
      pendingPersonSaves.delete(key);
      node.disabled = false;
      node.textContent = "Save";
      node.removeAttribute?.("aria-busy");
      if (state.packet?.company_key === packet.company_key) renderPacket();
      const failure = `Could not save ${person.name}: ${error.message}`;
      if (callbacks.notify) callbacks.notify(failure, "error");
      else message(failure);
    }
    return;
  }
  if (action === "mission-research") {
    if (missionResearchPending || ["beings", "all"].includes(missionId)) return;
    missionResearchPending = true;
    node.disabled = true;
    try {
      const result = await api("/api/scans/mission", {mission_id:missionId, company_count:25});
      if (result.companies) {
        callbacks.scanStarted?.(result);
        message(`Website research started for ${result.companies} companies.`);
      } else {
        message("No company websites are due for this mission.");
      }
    } finally {
      missionResearchPending = false;
      node.disabled = false;
      renderList();
    }
    return;
  }
  if(action === "tag-rescore" || action === "tag-rescan") {
    if (!selectedTag || tagMutation) return;
    const tag = selectedTag, selectedMission = missionId;
    tagMutation = true;
    node.disabled = true;
    node.textContent = action === "tag-rescore" ? "Re-scoring…" : "Starting…";
    try {
      const result = await api(action === "tag-rescore" ? "/tags/rescore" : "/api/scans/tag",
        {tag, mission_id:selectedMission});
      if (action === "tag-rescan") callbacks.scanStarted?.(result);
      if (selectedTag === tag && missionId === selectedMission) await loadSalesWorkbench({invalidate:true});
      message(action === "tag-rescore"
        ? `${tag}: ${result.refreshed} companies re-scored${result.errors.length ? `; ${result.errors.length} failed` : ""}${result.unscanned ? `; ${result.unscanned} need a scan` : ""}.`
        : `${tag}: re-scan started for ${result.companies} companies.`);
    } finally {
      tagMutation = false;
      node.disabled = false;
      node.textContent = action === "tag-rescore" ? "Re-score tag" : "Re-scan tag";
      renderList();
    }
  }
  if(action === "company-tag-toggle") {
    const panel = document.querySelector("#engine-tag-popover");
    panel.hidden = !panel.hidden;
    node.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) document.querySelector("#engine-company-tag-input")?.focus();
  }
  if(action === "company-tag-add") await changeCompanyTag(node.dataset.tag, "add");
  if(action === "company-star") await changeCompanyTag("starred", (state.packet?.tags || []).includes("starred") ? "remove" : "add");
  if(action === "company-tag-remove") await changeCompanyTag(node.dataset.tag, "remove");
  if(action === "mission-manage")openMissionDialog("manage");
  if(action === "mission-select") {closeMissionDialog();await changeMission(node.dataset.id);}
  if(action === "mission-remove") await hideMission(node.dataset.id,true);
  if(action === "mission-restore") await hideMission(node.dataset.id,false);
  if(action === "mission-delete")openMissionDialog("delete",node.dataset.id);
  if(action === "mission-delete-confirm")await deleteMission(node.dataset.id);
  if(action === "mission-new")openMissionDialog("new");
  if(action === "mission-edit")openMissionDialog("edit",node.dataset.id);
  if(action === "mission-compile")openMissionDialog("compile",node.dataset.id || missionId);
  if(action === "mission-cancel")closeMissionDialog();
  if (action === "emails" || action === "source-contacts") {
    const sourceOnly = action === "source-contacts";
    const p = state.packet;
    if (!p?.candidate_id || safeSummary(p).summary_loading || isLoading("summary")
        || emailLookupStages.has(p.candidate_id)) return;
    const updateStage = label => {
      emailLookupStages.set(p.candidate_id, {action, label});
      syncContactLookupButton();
    };
    const notify = (text, tone) => {
      if (callbacks.notify) callbacks.notify(text, tone);
      else if (state.packet?.candidate_id === p.candidate_id) message(text);
    };
    const lookupMission = missionId;
    const summaryPath = missionPath(`/companies/${p.candidate_id}/summary`);
    updateStage("Starting…");
    try {
      renderPacket();
      callbacks.contactLookupChanged?.();
      const estimate = await api(`/companies/${p.candidate_id}/emails/estimate`);
      const treg = p.email_lookup?.configured === true;
      const maximum = treg && !estimate.people_checked_without_candidates
        ? (sourceOnly ? estimate.people_maximum_microusd : estimate.maximum_microusd) || 0 : 0;
      const microlinkRequests = p.email_lookup?.microlink_configured
        ? estimate.microlink_max_requests : 0;
      if ((!treg || sourceOnly) && !maximum && !microlinkRequests) {
        notify(sourceOnly ? "Contact sources are already current." : "No contact search is due.", "info");
        return;
      }
      let emails = {actual_cost_usd:0};
      if (maximum || microlinkRequests) {
        updateStage(sourceOnly ? "Sourcing…"
          : maximum && microlinkRequests ? "Checking emails and websites…"
          : microlinkRequests ? "Checking company website…" : "Finding contacts…");
        try {
          emails = await runCompanyContactLookup(
            p.candidate_id, maximum, updateStage, microlinkRequests, sourceOnly,
          );
        } catch (error) {
          emails = {actual_cost_usd:null, error:true, message:error.message};
        }
      }
      let phones;
      if (!sourceOnly && treg && !emails.waiting_for_people) {
        try {
          phones = await runCompanyPhoneLookup(p, summaryPath, lookupMission, updateStage);
        } catch {
          phones = {failures:1, knownCost:0, chargeUnknown:false};
        }
      }
      const knownEmailCost = Number.isFinite(emails.actual_cost_usd);
      const total = (knownEmailCost ? Math.round(emails.actual_cost_usd * 1_000_000) : 0)
        + (phones?.knownCost || 0);
      const unconfirmed = !knownEmailCost || phones?.chargeUnknown;
      const charge = unconfirmed
        ? `${contactCharge(total)} confirmed · total charge pending`
        : total ? `Total charged ${contactCharge(total)}` : "No charge";
      const microlink = emails.microlink_contact_search;
      const lookupErrors = [emails.error, ...(emails.errors || []), ...(emails.saved_errors || [])]
        .filter(Boolean);
      const emailStatus = lookupErrors.some(error => error.kind === "timeout")
        ? sourceOnly ? "Contact sourcing timed out" : "Email lookup timed out"
        : lookupErrors.length || !knownEmailCost
          ? sourceOnly ? "Contact sourcing incomplete" : "Email lookup incomplete"
          : emails.waiting_for_people ? "Contact search still running"
          : (emails.message || "").startsWith("Saved email results or reserved attempts reused")
            ? "No new email search was run." : microlink ? "" : emails.message || "";
      const details = [
        phones?.found ? `${phones.found} phone ${phones.found === 1 ? "number" : "numbers"} saved` : "",
        emailStatus,
        phones?.failures ? "We couldn’t finish checking for phone numbers." : "",
        microlink?.status === "failed" ? "Website contact search failed"
          : microlink?.status === "partial" ? "Website contact search incomplete" : "",
        charge,
        microlink?.requests ? `${microlink.requests} Microlink requests from subscription` : "",
      ];
      if (state.packet?.candidate_id === p.candidate_id) {
        void loadSalesWorkbench({invalidate:true, background:true});
      }
      notify(details.filter(Boolean).join(" · "),
        unconfirmed || lookupErrors.length || phones?.failures || ["partial", "failed"].includes(microlink?.status)
          ? "error" : emails.waiting_for_people ? "info" : "success");
    } catch (error) {
      if (!cancelled(error)) notify("We couldn’t finish the contact search.", "error");
    } finally {
      emailLookupStages.delete(p.candidate_id);
      if (state.packet?.candidate_id === p.candidate_id) renderPacket();
      callbacks.contactLookupChanged?.();
    }
  }
  if (action === "company") await loadPacket(node.dataset.id);
  if (action === "pipeline-company") await loadPacket(node.dataset.id, true);
  if (action === "previous" || action === "next") await changePage(Math.floor(listOffset/pageSize)+1+(action === "next" ? 1 : -1));
  if (action === "import") callbacks.import?.();
  if (action === "export") callbacks.export?.(missionId === "beings" ? undefined :
    `/api/sales-engine/missions/${encodeURIComponent(missionId)}/export.csv?search=${encodeURIComponent(state.search)}&saved=${state.tab === "pipeline"}`);
}
export async function loadSalesWorkbench({invalidate = false, refreshSummary = true, background = false} = {}) {
  const opening = !active;
  active = true;
  renderMissions();
  if (invalidate) invalidateSalesWorkbench();
  const refresh = ++overviewNumber, tab = state.tab;
  if (needsSalesMission()) {
    state.tab = "queue";
    if (!rendered) render();
    else { renderList(); renderPacket(); }
    if (opening) document.querySelector("#engine-welcome-title")?.focus();
    return;
  }
  if (!background) api("/api/tags").then(data => {
    if (refresh !== overviewNumber || !active) return;
    companyTags = data.items || []; renderList(true);
  }).catch(error => { if (refresh === overviewNumber) message(error.message); });
  const path = missionPath(tab === "pipeline"
    ? `/pipeline?compact=true&search=${encodeURIComponent(state.search)}&stage=${encodeURIComponent(pipelineStage)}${selectedTag ? `&tag=${encodeURIComponent(selectedTag)}` : ""}&offset=${listOffset}&limit=${pageSize}`
    : `/overview?compact=true&search=${encodeURIComponent(state.search)}&prospects_only=false${selectedTag ? `&tag=${encodeURIComponent(selectedTag)}` : ""}&offset=${listOffset}&limit=${pageSize}${companySort === "newest" ? "&sort=newest" : ""}`);
  const cached = responseCache.get(path);
  const changingList = currentListPath !== path;
  currentListPath = path;
  if (cached) {
    const value = cached.value;
    if (tab === "pipeline") pipelineData = value; else state.overview = value;
  } else if (tab === "pipeline" && (!pipelineData || changingList)) pipelineData = {items:[],total:0,loading:true};
  else if (tab === "queue" && (!state.overview || changingList)) state.overview = {items:[],loading:true};
  if (!rendered) render(); else if (!background) { renderList(); if (!state.packet || expired(state.packet)) renderPacket(); }
  // Refresh the already selected summary independently of the list response.
  const selected = selectedKey;
  const summaryRefresh = selected && refreshSummary ? loadPacket(Number(selected.split("/").pop()), selected.startsWith("/pipeline/"), {refresh:true}) : Promise.resolve();
  const selection = requestNumber;
  const controller = startRequest("list", path);
  const listRefresh = (async () => {
    try {
      const value = await cachedRead(path, controller);
      if (refresh !== overviewNumber || tab !== state.tab || !active) return;
      const lastOffset = Math.max(0, Math.ceil((value.total || 0)/pageSize)-1)*pageSize;
      if (typeof value.total === "number" && listOffset > lastOffset) {
        listOffset = lastOffset;
        await loadSalesWorkbench();
        return;
      }
      if (tab === "pipeline") pipelineData = value; else state.overview = value;
      if (isConnectionInterrupted(state.message) || isLoadingMessage(state.message)) message("");
      observeRankingProgress(value);
      if (value.compilation) missions=missions.map(m=>m.id===missionId ? {...m,compilation:value.compilation} : m);
      renderList(background); armExpiry();
      if (selection !== requestNumber || selectedKey) return;
      const first = value.items?.[0];
      const isSaved = tab === "pipeline" || !first?.candidate_id;
      const id = isSaved ? first?.account_id : first?.candidate_id;
      if (id) await loadPacket(id, isSaved);
      else { state.packet = null; renderPacket(); }
    } catch (error) {
      if (!cancelled(error) && refresh === overviewNumber && active) {
        const data = tab === "pipeline" ? pipelineData : state.overview;
        if (data?.loading) { data.loading = false; data.load_error = true; renderList(); }
        if (!background) message(error.message);
        armExpiry();
      }
    }
  })();
  await Promise.all([listRefresh, summaryRefresh]);
}
