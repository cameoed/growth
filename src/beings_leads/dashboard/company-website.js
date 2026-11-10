// Persistent browsing, independent of company-detail renders and research.
export function publicWebsite(value) {
  try {
    const url = new URL(value), host = url.hostname.toLowerCase().replace(/\.$/, "");
    if (!/^https?:$/.test(url.protocol) || url.username || url.password
        || !host.includes(".") || /[\[\]:]/.test(host) || /^[\d.]+$/.test(host)
        || /(?:^|\.)(?:localhost|local|internal|lan|home|invalid)$/.test(host)) return "";
    return url.href;
  } catch { return ""; }
}
/* Lucide icons · https://lucide.dev/license
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

---

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
const icons = {
  "back":'<path d="m12 19-7-7 7-7" /><path d="M19 12H5" />',
  "forward":'<path d="M5 12h14" /><path d="m12 5 7 7-7 7" />',
  "reload":'<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" /><path d="M21 3v5h-5" />',
  "home":'<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8" /><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />',
  "expand":'<path d="M8 3H5a2 2 0 0 0-2 2v3" /><path d="M21 8V5a2 2 0 0 0-2-2h-3" /><path d="M3 16v3a2 2 0 0 0 2 2h3" /><path d="M16 21h3a2 2 0 0 0 2-2v-3" />',
  "restore":'<path d="M8 3v3a2 2 0 0 1-2 2H3" /><path d="M21 8h-3a2 2 0 0 1-2-2V3" /><path d="M3 16h3a2 2 0 0 1 2 2v3" /><path d="M16 21v-3a2 2 0 0 1 2-2h3" />',
  "external":'<path d="M7 17 17 7M7 7h10v10" />',
  "zoom-out":'<path d="M5 12h14" />',
  "zoom-in":'<path d="M5 12h14" /><path d="M12 5v14" />',
  "audio":'<path d="M11 4.702a.7.7 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.7.7 0 0 0 11 19.298z" /><path d="m16.5 14.5 5-5" /><path d="m16.5 9.5 5 5" />',
  "audible":'<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z" /><path d="M16 9a5 5 0 0 1 0 6" /><path d="M19.364 18.364a9 9 0 0 0 0-12.728" />',
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[name]}</svg>`;
const iconButton = (action, label, attributes = "") => `<button type="button" class="button" data-website="${action}" aria-label="${label}" title="${label}" ${attributes}>${icon(action)}</button>`;

const clamp = (value, min, max) => Math.round(Math.max(min, Math.min(max, value)));

function measurePaneWidth(layout, divider, leftPane) {
  const available = layout.clientWidth - divider.offsetWidth;
  return {horizontal:true, size:leftPane.offsetWidth,
    min:Math.min(232, Math.floor(available * .42)), max:available - Math.min(320, Math.floor(available * .48))};
}

function announce(divider, {min, max}, size) {
  divider.setAttribute("aria-valuemin", min);
  divider.setAttribute("aria-valuemax", max);
  divider.setAttribute("aria-valuenow", clamp(size, min, max));
}

function bindPaneDivider(divider, measure, apply, save, reset, refresh) {
  let drag;
  let pending;
  let frame = 0;
  const coordinate = (event, {horizontal}) => horizontal ? event.clientX : event.clientY;
  const flush = () => {
    frame = 0;
    if (drag && pending !== undefined) {
      drag.size = apply(drag.limits, pending);
      pending = undefined;
    }
  };
  divider.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !event.isPrimary) return;
    const limits = measure();
    drag = {id:event.pointerId, limits, start:coordinate(event, limits)};
    divider.setPointerCapture(event.pointerId);
    divider.classList.add("is-dragging");
    if (document.activeElement === divider) divider.blur();
    event.preventDefault();
  });
  divider.addEventListener("pointermove", event => {
    if (event.pointerId !== drag?.id) return;
    pending = drag.limits.size + coordinate(event, drag.limits) - drag.start;
    if (!frame) frame = requestAnimationFrame(flush);
  });
  divider.addEventListener("lostpointercapture", event => {
    if (event.pointerId !== drag?.id) return;
    cancelAnimationFrame(frame);
    flush();
    if (drag.size !== undefined) save(drag.limits, drag.size);
    drag = undefined;
    divider.classList.remove("is-dragging");
    refresh();
  });
  divider.addEventListener("dblclick", () => {
    reset(measure());
    refresh();
  });
  divider.addEventListener("keydown", event => {
    const limits = measure();
    const less = limits.horizontal ? "ArrowLeft" : "ArrowUp";
    const more = limits.horizontal ? "ArrowRight" : "ArrowDown";
    const sizes = {Home:limits.min, End:limits.max, [less]:limits.size - 16, [more]:limits.size + 16};
    if (event.metaKey || event.ctrlKey || event.altKey || !Object.hasOwn(sizes, event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    save(limits, apply(limits, sizes[event.key]));
  });
}

export function installScanPaneDivider(layout) {
  const controls = layout.querySelector("#smart-scan-controls");
  const divider = layout.querySelector(".engine-pane-divider");
  const stacked = matchMedia("(max-width: 760px)");
  const preferences = {};
  for (const axis of ["width", "height"]) {
    try {
      const value = Number(localStorage.getItem(`scan.controls-${axis}`));
      if (Number.isFinite(value) && value > 0) preferences[axis] = value;
    } catch { /* Storage can be disabled. */ }
  }
  function measure() {
    if (stacked.matches) {
      const max = Math.max(220, Math.floor(innerHeight * .75));
      return {axis:"height", horizontal:false, size:controls.offsetHeight, min:150, max};
    }
    return {...measurePaneWidth(layout, divider, controls), axis:"width"};
  }
  function apply(limits, value) {
    const size = clamp(value, limits.min, limits.max);
    layout.style.setProperty(limits.horizontal ? "--split-pane-width" : "--scan-controls-height", `${size}px`);
    announce(divider, limits, size);
    return size;
  }
  function save({axis}, size) {
    preferences[axis] = size;
    try { localStorage.setItem(`scan.controls-${axis}`, size); } catch { /* Optional preference. */ }
  }
  function reset({axis, horizontal}) {
    delete preferences[axis];
    layout.style.removeProperty(horizontal ? "--split-pane-width" : "--scan-controls-height");
    try { localStorage.removeItem(`scan.controls-${axis}`); } catch { /* Optional preference. */ }
  }
  function refresh() {
    if (!layout.clientWidth || divider.classList.contains("is-dragging")) return;
    const limits = measure();
    divider.setAttribute("aria-orientation", limits.horizontal ? "vertical" : "horizontal");
    if (preferences[limits.axis] !== undefined) apply(limits, preferences[limits.axis]);
    else announce(divider, limits, limits.size);
  }
  bindPaneDivider(divider, measure, apply, save, reset, refresh);
  const observer = new ResizeObserver(refresh);
  observer.observe(layout);
  observer.observe(controls);
  refresh();
}

function installPaneDividers(host, viewport) {
  const layout = host.closest(".engine-layout");
  const workspace = host.parentElement;
  const list = layout?.querySelector("#engine-list");
  const listDivider = list?.nextElementSibling;
  const websiteDivider = host.nextElementSibling;
  if (!listDivider?.matches(".engine-pane-divider") || !websiteDivider?.matches(".engine-pane-divider")) return;

  const toolbar = host.querySelector(".engine-website-toolbar");
  const summary = workspace.querySelector("#engine-company-header");
  const stacked = matchMedia("(max-width: 760px)");
  const preferences = {};
  for (const name of ["company-list-width", "company-list-height", "website-height"]) {
    try {
      const value = Number(localStorage.getItem(`companies.${name}`));
      if (Number.isFinite(value) && value > 0) preferences[name] = value;
    } catch { /* Storage can be disabled. */ }
  }
  function measure(divider) {
    if (divider === websiteDivider) {
      const chrome = toolbar.offsetHeight + 2;
      const available = workspace.clientHeight - (summary?.offsetHeight ?? 0) - divider.offsetHeight;
      const room = available - chrome;
      const max = stacked.matches ? Math.max(310, Math.floor(innerHeight * .8))
        : Math.max(0, room - Math.min(132, Math.floor(room * .32)));
      return {name:"website-height", horizontal:false, size:viewport.clientHeight,
        min:Math.min(120, max), max};
    }
    if (stacked.matches) {
      const size = list.querySelector(".engine-company-list")?.clientHeight ?? 0;
      return {name:"company-list-height", horizontal:false, size, min:90, max:Math.max(220, Math.floor(innerHeight * .75))};
    }
    return {...measurePaneWidth(layout, divider, list), name:"company-list-width"};
  }
  function apply(divider, limits, value) {
    const size = clamp(value, limits.min, limits.max);
    layout.style.setProperty(limits.horizontal ? "--split-pane-width" : `--${limits.name}`, `${size}px`);
    if (limits.name === "company-list-height") layout.classList.add("has-list-height");
    announce(divider, limits, size);
    return size;
  }
  function save({name}, size) {
    preferences[name] = size;
    try { localStorage.setItem(`companies.${name}`, size); } catch { /* Optional preference. */ }
  }
  function reset(limits) {
    const {name, horizontal} = limits;
    if (name === "website-height") {
      const tallerHeight = Math.max(320, Math.round(innerHeight * 0.44));
      save(limits, apply(websiteDivider, limits, tallerHeight));
      return;
    }
    delete preferences[name];
    layout.style.removeProperty(horizontal ? "--split-pane-width" : `--${name}`);
    if (name === "company-list-height") layout.classList.remove("has-list-height");
    try { localStorage.removeItem(`companies.${name}`); } catch { /* Optional preference. */ }
  }
  function refresh() {
    if (!layout.clientWidth) return;
    listDivider.setAttribute("aria-orientation", stacked.matches ? "horizontal" : "vertical");
    for (const divider of [listDivider, websiteDivider]) {
      if (divider.classList.contains("is-dragging")) continue;
      const limits = measure(divider);
      if (divider === websiteDivider && !stacked.matches) {
        layout.style.setProperty("--website-height-max", `${limits.max}px`);
      }
      if (preferences[limits.name] !== undefined) apply(divider, limits, preferences[limits.name]);
      else announce(divider, limits, measure(divider).size);
    }
  }
  for (const divider of [listDivider, websiteDivider]) {
    bindPaneDivider(divider, () => measure(divider), (limits, size) => apply(divider, limits, size), save, reset, refresh);
  }
  const observer = new ResizeObserver(refresh);
  for (const element of [layout, toolbar]) observer.observe(element);
  if (summary) observer.observe(summary);
  window.addEventListener("resize", refresh);
  refresh();
}

let zoom = .8;
let muted = true;

export function createCompanyWebsite(host) {
  const native = window.webkit?.messageHandlers?.companyWebsite;
  host.className = "panel engine-website";
  host.innerHTML = `<header class="engine-website-toolbar">
    <div class="engine-website-navigation">${Object.entries({back:"Back", forward:"Forward", reload:"Reload website", home:"Company homepage"}).map(([action, label]) => iconButton(action, label, "disabled")).join("")}</div>
    <form class="engine-website-address"><label class="sr-only" for="company-website-url">Website address</label><input id="company-website-url" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="Company website" disabled>
      <a class="engine-website-external" target="_blank" rel="noopener noreferrer" aria-label="Open website in browser" title="Open website in browser" hidden>${icon("external")}</a>
    </form>
    <div class="engine-website-actions">
      <div class="engine-website-zoom" role="group" aria-label="Website zoom controls">${iconButton("zoom-out", "Zoom website out", "disabled")}<output aria-label="Website zoom">${Math.round(zoom * 100)}%</output>${iconButton("zoom-in", "Zoom website in", "disabled")}</div>
      ${native ? iconButton("audio", "Website sound", 'aria-pressed="false" disabled') : ""}
      ${iconButton("expand", "Expand website", 'aria-pressed="false"')}
    </div>
    <span class="engine-website-status sr-only" role="status"></span>
  </header><div class="engine-website-viewport"><p class="engine-website-placeholder" role="status" hidden></p></div>`;
  const viewport = host.querySelector(".engine-website-viewport"), address = host.querySelector("input");
  const external = host.querySelector("a"), status = host.querySelector(".engine-website-status");
  const audio = host.querySelector('[data-website="audio"]');
  function showStatus(text, visible = false) {
    status.textContent = text;
    status.classList.toggle("sr-only", !visible);
  }
  const placeholder = host.querySelector(".engine-website-placeholder");
  installPaneDividers(host, viewport);
  let company = null, homepage = "", current = "", frame = null;
  let history = [], position = -1, token = 0, expanded = false, scheduled = false;
  let canBack = false, canForward = false;
  const activate = native ? null : document.createElement("button");
  function setFrameInteractive(interactive) {
    if (!frame) return;
    frame.inert = !interactive;
    activate.hidden = interactive;
  }
  if (activate) {
    activate.type = "button";
    activate.className = "engine-website-activate";
    activate.innerHTML = "<span>Interact with website</span>";
    activate.hidden = true;
    viewport.append(activate);
    let dashboardFocus = document.activeElement;
    activate.addEventListener("click", () => {
      if (!frame) return;
      setFrameInteractive(true);
      frame.focus({preventScroll:true});
    });
    document.addEventListener("focusin", event => {
      if (event.target === frame) return;
      dashboardFocus = event.target;
      setFrameInteractive(false);
    });
    document.addEventListener("pointerdown", () => setFrameInteractive(false), true);
    window.addEventListener("blur", () => {
      // Some browsers let a cross-origin document focus itself even in an inert iframe.
      requestAnimationFrame(() => {
        if (!frame?.inert || document.activeElement !== frame || !document.hasFocus()) return;
        const target = dashboardFocus?.isConnected && dashboardFocus !== document.body
          ? dashboardFocus : document.querySelector('.engine-company[aria-pressed="true"]');
        target?.focus({preventScroll:true});
      });
    });
  }
  const send = data => native?.postMessage({...data, token});
  function controls() {
    for (const button of host.querySelectorAll("[data-website]")) {
      const action = button.dataset.website;
      button.disabled = action === "back" ? !(native ? canBack : position > 0)
        : action === "forward" ? !(native ? canForward : position < history.length - 1)
        : action === "expand" ? false : !current;
    }
    address.disabled = !homepage; external.hidden = !current;
    if (current) external.href = current; else external.removeAttribute("href");
    if (document.activeElement !== address) address.value = current;
    host.querySelector("output").textContent = `${Math.round(zoom * 100)}%`;
    if (audio) {
      audio.setAttribute("aria-pressed", String(!muted));
      audio.title = muted ? "Unmute website" : "Mute website";
      audio.innerHTML = icon(muted ? "audio" : "audible");
    }
  }
  let reconnecting = false;
  function geometry() {
    scheduled = false;
    if (!native) return;
    const rect = viewport.getBoundingClientRect();
    const visible = Boolean(current && !reconnecting && host.getClientRects().length
      && !document.querySelector("dialog[open]") && !document.hidden);
    let top = Math.max(0, rect.top), left = Math.max(0, rect.left);
    let right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
    // Clip native content to the same scroll containers as the HTML placeholder.
    if (!expanded) for (let parent = viewport.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
      if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
        top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom);
      }
      if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
        left = Math.max(left, bounds.left); right = Math.min(right, bounds.right);
      }
    }
    send({action:"bounds", visible:visible && right > left && bottom > top, x:left, y:top,
      width:Math.max(0, right - left), height:Math.max(0, bottom - top),
      contentWidth:rect.width, contentHeight:rect.height, insetX:left - rect.left, insetY:top - rect.top,
      bottomRadius:parseFloat(getComputedStyle(viewport).borderBottomLeftRadius),
      viewportWidth:innerWidth, viewportHeight:innerHeight});
  }
  function scheduleGeometry() {
    if (!scheduled) { scheduled = true; requestAnimationFrame(geometry); }
  }
  function setExpanded(value) {
    expanded = value; host.classList.toggle("is-expanded", value);
    const button = host.querySelector('[data-website="expand"]');
    button.setAttribute("aria-pressed", String(value));
    const label = value ? "Restore website pane" : "Expand website";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.innerHTML = icon(value ? "restore" : "expand");
    scheduleGeometry();
  }
  function exitWebsite() {
    if (!host.getClientRects().length || document.querySelector("dialog[open]")) return false;
    if (expanded) {
      setExpanded(false);
      host.querySelector('[data-website="expand"]').focus({preventScroll:true});
    } else {
      const row = document.querySelector('.engine-company[aria-pressed="true"]');
      if (!row) return false;
      row.focus();
    }
    return true;
  }
  function applyFrameZoom() {
    frame.style.width = `${100 / zoom}%`;
    frame.style.height = `${100 / zoom}%`;
    frame.style.transform = `scale(${zoom})`;
    frame.style.transformOrigin = "0 0";
  }
  function navigate(value, {record = true} = {}) {
    const url = publicWebsite(value);
    if (!url) { showStatus("Enter a public http or https website.", true); return; }
    current = url;
    if (record) { history = history.slice(0, position + 1); history.push(url); position++; }
    showStatus("Loading website…"); placeholder.hidden = true; controls();
    if (native) { send({action:"navigate", url, zoom, muted}); scheduleGeometry(); return; }
    if (!frame) {
      frame = document.createElement("iframe"); frame.title = "Company website";
      frame.setAttribute("sandbox", "allow-scripts allow-forms allow-popups");
      frame.setAttribute("allow", "autoplay 'none'");
      frame.referrerPolicy = "no-referrer";
      frame.addEventListener("load", () => showStatus("Website frame · if unavailable, open ↗"));
      frame.addEventListener("error", () => showStatus("Website unavailable · open ↗", true));
      applyFrameZoom();
      viewport.append(frame);
    }
    setFrameInteractive(false);
    frame.src = url;
  }
  host.querySelector("form").addEventListener("submit", event => {
    event.preventDefault();
    const value = address.value.trim();
    address.blur(); navigate(/^[a-z][a-z\d+.-]*:/i.test(value) ? value : `https://${value}`);
  });
  host.addEventListener("click", event => {
    const button = event.target.closest("[data-website]");
    if (!button || button.disabled) return;
    const action = button.dataset.website;
    if (action === "expand") { setExpanded(!expanded); return; }
    if (action === "audio") {
      muted = !muted;
      send({action:"audio", muted});
      controls(); return;
    }
    if (action.startsWith("zoom-")) {
      zoom = Math.round(Math.max(.5, Math.min(2, zoom + (action === "zoom-in" ? .1 : -.1))) * 10) / 10;
      if (native) send({action:"zoom", zoom});
      else if (frame) applyFrameZoom();
      controls(); return;
    }
    if (native && ["back", "forward", "reload"].includes(action)) { send({action}); return; }
    if (action === "home") navigate(homepage);
    else if (action === "back" || action === "forward") {
      position += action === "back" ? -1 : 1; navigate(history[position], {record:false});
    } else if (action === "reload") navigate(current, {record:false});
  });
  window.addEventListener("beings-website-state", event => {
    if (!native || event.detail?.token !== token) return;
    const data = event.detail;
    if (publicWebsite(data.url)) current = data.url;
    canBack = data.canBack; canForward = data.canForward;
    showStatus(data.error || (data.loading ? "Loading website…" : ""), Boolean(data.error)); controls();
  });
  if (native) window.addEventListener("beings-website-reconnect", event => {
    reconnecting = event.detail === true;
    scheduleGeometry();
  });
  window.addEventListener("beings-website-escape", event => {
    if (native && event.detail === token) exitWebsite();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !event.repeat && !event.isComposing
        && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (expanded || host.contains(event.target)) && exitWebsite()) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  host.addEventListener("keydown", event => {
    event.stopPropagation(); // Website controls must not select another company.
  });
  const geometryObserver = new ResizeObserver(scheduleGeometry);
  geometryObserver.observe(viewport);
  geometryObserver.observe(host.querySelector(".engine-website-toolbar"));
  if (native) new MutationObserver(scheduleGeometry).observe(document.body,
    {subtree:true, childList:true, attributes:true, attributeFilter:["class", "open", "hidden", "style"]});
  window.addEventListener("resize", scheduleGeometry);
  document.addEventListener("scroll", scheduleGeometry, true);
  document.addEventListener("visibilitychange", scheduleGeometry);
  return {
    select(packet) {
      const key = packet?.company_key || null;
      if (key === company) return;
      company = key; token++; history = []; position = -1;
      canBack = canForward = false;
      homepage = key ? publicWebsite(`https://${key}`) : "";
      current = ""; frame?.remove(); frame = null;
      if (activate) activate.hidden = true;
      if (native) send({action:"clear"});
      setExpanded(false);
      if (homepage) navigate(homepage);
      else {
        placeholder.hidden = !key;
        placeholder.textContent = key ? "No public website available." : "";
        showStatus(""); controls(); scheduleGeometry();
      }
    },
  };
}
