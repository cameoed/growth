const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const source=fs.readFileSync('src/beings_leads/dashboard/app.js','utf8');
const a=source.indexOf('function syncScanGoalHelper(dialog, mode) {');
const b=source.indexOf('function bindMicrolinkScanBar(',a);
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const page=await browser.newPage();
  await page.setContent('<style>'+fs.readFileSync('src/beings_leads/dashboard/styles.css','utf8')+'</style><div id="scan-smart-mode"><button id="scan-launch-trigger">Smart Scan</button><button id="import-companies-trigger">Import companies</button><dialog id="import-companies-dialog" class="modal smart-scan-options-dialog"><header class="modal-head">Import companies</header><div class="scan-setup-body"><form id="import-scan-form"><textarea name="urls"></textarea></form></div><footer class="modal-actions">Close</footer></dialog></div>');
  await page.evaluate(code=>{
   const $=s=>document.querySelector(s);
   eval(code+';window.importMode=setScanImportMode;');
   window.importMode(true);
  },source.slice(a,b));
  assert(await page.locator('dialog').isVisible());
  assert(await page.locator('#scan-smart-mode').isVisible());
  assert.equal(await page.locator('dialog #import-scan-form').count(),1);
  await page.locator('[name=urls]').fill('company.example');
  await page.evaluate(()=>window.importMode(false));
  assert.equal(await page.locator('dialog').isVisible(),false);
  assert.equal(await page.locator('[name=urls]').inputValue(),'company.example');
  await page.evaluate(()=>window.importMode(true));
  assert(await page.locator('[name=urls]').evaluate(el => el === document.activeElement));
  console.log('Import opens in its own modal; workspace and URL draft stay intact.');
 }finally{await browser.close();}
})();
