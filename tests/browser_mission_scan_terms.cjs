const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const source=fs.readFileSync('src/beings_leads/dashboard/app.js','utf8');
const code=source.slice(source.indexOf('function selectedScanMission('),source.indexOf('function bindScanGoal('));
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const page=await browser.newPage();
  await page.route('http://mission.test/**',route=>route.fulfill({contentType:'text/html',body:'<dialog><textarea id="smart-scan-discovery-terms"></textarea><textarea name="meta_queries">generic ads</textarea></dialog>'}));
  await page.goto('http://mission.test/');
  const result=await page.evaluate(async code=>{
   const $=(s,r=document)=>r.querySelector(s),state={};
   const DEFAULT_META_DISCOVERY_QUERIES=['generic ads'];
   let terms=['industrial robotics','automation equipment'];
   const calls=[];
   const api=async url=>{calls.push(url);return {mission_id:localStorage.getItem('beings-mission'),search_terms:terms};};
   eval(code+';window.seed=seedMissionScanTerms;');
   const dialog=$('dialog');dialog.showModal();
   localStorage.setItem('beings-mission','robotics');
   await window.seed(dialog);
   const first=$('#smart-scan-discovery-terms').value;
   const firstAds=$('[name=meta_queries]').value;
   terms=['corporate event venues'];localStorage.setItem('beings-mission','events');
   await window.seed(dialog);
   const switched=$('#smart-scan-discovery-terms').value;
   $('#smart-scan-discovery-terms').value='operator entered term';
   $('[name=meta_queries]').value='operator paid query';
   terms=['new compiled term'];await window.seed(dialog);
   return {first,firstAds,switched,manual:$('#smart-scan-discovery-terms').value,manualAds:$('[name=meta_queries]').value,calls};
  },code);
  assert.equal(result.first,'industrial robotics\nautomation equipment');
  assert.equal(result.firstAds,result.first);
  assert.equal(result.switched,'corporate event venues');
  assert.equal(result.manual,'operator entered term');assert.equal(result.manualAds,'operator paid query');
  assert(result.calls[0].includes('mission_id=robotics'));
  console.log('Mission query defaults seed and follow mission changes; operator edits remain intact.');
 } finally {await browser.close();}
})();
