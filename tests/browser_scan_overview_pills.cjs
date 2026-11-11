const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/beings_leads/dashboard/app.js', 'utf8');
const overview = {hidden:false,innerHTML:'',dataset:{}};
const row = (label, status, details = {}, parent = false) => ({label,status,run:{status,...details},parent});
const failed = row('Customer Lists','failed',{id:9,candidates_persisted:0});
const oldCustomers = row('Customer Lists','complete',{id:8,candidates_persisted:4});
const oldGraph = row('Public Graph','complete',{id:7,candidates_persisted:3});
const rows = [failed,oldCustomers,oldGraph];
const context = vm.createContext({
  state:{activeView:'smart_scan'}, standaloneScanFocus:null,
  ACTIVE_RUN_STATES:new Set(['running','queued','starting']),
  smartScanActive:run=>['running','starting'].includes(run.status),scanOverviewItems:()=>rows,
  renderStandaloneScanWorkspace:()=>{},$:selector=>selector==='#scan-overview'?overview:null,
  smartScanProgress:()=>12.5,discoveryProgressPercent:run=>run.progress_percent ?? null,
  formatNumber:String,humanize:s=>s==='failed'?'Failed':s,
  escapeHtml:String,firstValue:(obj,paths,fallback)=>{
    for(const path of paths){const value=path.split('.').reduce((o,k)=>o?.[k],obj);if(value!=null)return value;}
    return fallback;
  },
});
vm.runInContext(source.slice(source.indexOf('function scanPillSuccesses('),source.indexOf('function adDiscoveryConfig(')),context);

context.renderAggregateScanState();
assert.equal(overview.hidden,true,'History alone leaves the overview hidden');
context.standaloneScanFocus = failed;
context.renderAggregateScanState();
assert.match(overview.innerHTML,/Failed/);
assert.doesNotMatch(overview.innerHTML,/0 companies saved|4 companies saved|Public Graph/);

const current = row('Customer Lists','running',{id:10,candidates_persisted:2,progress_percent:31});
rows.push(current);
context.standaloneScanFocus = current;
context.renderAggregateScanState();
assert.match(overview.innerHTML,/31%/);
assert.doesNotMatch(overview.innerHTML,/scan-overview-orb/);
assert.match(overview.innerHTML,/2 companies saved/);
assert.doesNotMatch(overview.innerHTML,/4 companies saved|Public Graph/);
current.status = current.run.status = 'complete';
context.renderAggregateScanState();
assert.match(overview.innerHTML,/2 companies saved/);

context.standaloneScanFocus = null;
rows.push(
  row('Smart Scan','running',{id:'smart-1',new_companies_added:0,paid_ads_run_id:41},true),
  row('Paid ads','complete',{id:41,candidates_persisted:6}),
  row('Paid ads','complete',{id:40,candidates_persisted:99}),
);
context.renderAggregateScanState();
assert.match(overview.innerHTML,/12.5%/);
assert.match(overview.innerHTML,/scan-overview-orb is-pulsing.*<strong title="Smart Scan"/);
assert.equal((overview.innerHTML.match(/scan-overview-orb/g)||[]).length,1);
assert.match(overview.innerHTML,/6 companies saved/);
assert.doesNotMatch(overview.innerHTML,/0 new companies|99 companies saved|Public Graph|Customer Lists/);

context.state.microlinkOverview = row('Microlink company search','running',{new_companies_added:1,progress_percent:30});
context.renderAggregateScanState();
assert.match(overview.innerHTML,/found 1 new company/);
assert.doesNotMatch(overview.innerHTML,/Smart Scan|Paid ads/);
assert.equal(rows.length,7);
console.log('Current scan identity, linked child progress, and isolated outcomes passed');
