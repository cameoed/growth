const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const source = fs.readFileSync('src/beings_leads/dashboard/app.js', 'utf8');
const highlight = source.slice(source.indexOf('function highlightLatestTerminalActivity('), source.indexOf('function updateSmartScanActivityClock('));
const helpers = highlight + source.slice(source.indexOf('let standaloneScanFocus = null;'), source.indexOf('function renderAggregateScanState()'));
(async () => {
  const browser = await chromium.launch({channel:'chrome', headless:true});
  try {
    const page = await browser.newPage();
    await page.setContent(`<section id="smart-scan-shell"><button id="scan-launch-trigger">Smart Scan</button><div class="smart-scan-progress-column"></div><div id="smart-terminal-lines"></div>${['error-count','meta','elapsed'].map(id=>`<span id="smart-terminal-${id}"></span>`).join('')}<button id="copy-smart-scan-errors"></button><p id="smart-terminal-error-note"></p></section>`);
    const result = await page.evaluate(code => {
      const state = {smartScanTerminalTab:'activity'};
      const ACTIVE_RUN_STATES = new Set(['starting','running','stopping','paused']);
      const $ = (selector, root=document) => root.querySelector(selector);
      const smartScanActive = run => ACTIVE_RUN_STATES.has(run.status);
      const firstValue = (run, keys, fallback) => keys.map(key=>key.split('.').reduce((v,k)=>v?.[k],run)).find(v=>v!=null) ?? fallback;
      const humanize = x=>x;
      const escapeHtml = x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;');
      const formatNumber = x=>String(x);
      const discoveryProgressPercent = run=>run.progress_percent ?? null;
      const smartScanTerminalLine = row=>`<div class="smart-terminal-line level-${row.level || "info"}">${escapeHtml(row.message)}</div>`;
      const smartScanTerminalNearBottom = ()=>false;
      const renderTerminalRows = (el, rows)=>{el.innerHTML=rows.join('');};
      const smartScanElapsedLabel = ()=>'1m';
      const renderSmartScan = ()=>{};
      return eval(code + `
        const parent={label:'Smart Scan',parent:true,status:'complete',run:{status:'complete'}};
        const kinds=['Web & CT','Import','Directories','Company intelligence','Tenant footprints','Portfolios','Customer lists','Searches','Public graph','Paid ads','Find sources','People'];
        for(const label of kinds) {
          const row={label,status:'running',run:{id:label,status:'running',processed:3,total_candidates:10,message:label+' current work'}};
          if(chooseStandaloneScan([parent,row]).label!==label) throw Error(label);
          renderStandaloneScanWorkspace([parent,row]);
          if(!$('#standalone-scan-progress').textContent.includes('3 / 10')) throw Error('Missing counts');
          if(!$('#smart-terminal-lines .is-current-activity')?.textContent.includes(label+' current work')) throw Error('Wrong activity');
          row.status=row.run.status='complete';
          if(chooseStandaloneScan([parent,row],row)!==row) throw Error('Completion lost');
        }
        if(chooseStandaloneScan([{...parent,run:{status:'running'} }],standaloneScanFocus)!==null) throw Error('Parent priority');
        if(chooseStandaloneScan([{label:'A',status:'running',run:{}},{label:'B',status:'running',run:{}}])!==null) throw Error('Multiple ambiguous scans');
        state.smartScanTerminalTab='errors';
        renderStandaloneScanWorkspace([{label:'Directories',status:'running',run:{id:'errors',errors:['Source unavailable'],message:'Checking'}}]);
        if($('#smart-terminal-lines .is-current-activity')) throw Error('Highlighted failure');
        if(!$('#smart-terminal-lines').textContent.includes('Source unavailable')) throw Error('Wrong failures');
        'All run types, progress, activity, failure isolation and parent priority passed.';
      `);
    }, helpers);
    console.log(result);
  } finally { await browser.close(); }
})();
