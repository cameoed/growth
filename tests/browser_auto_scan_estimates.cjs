// Scan estimate and launch requests are intercepted. No provider calls or database writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const dashboard = path.resolve(__dirname, '../src/beings_leads/dashboard');
const smart = {status:'idle', inventory_pending:false, inventory:{}, profile:{microlink_configured:true}};
const requests = [];
const hold = {};
hold.promise = new Promise(resolve => { hold.release = resolve; });
let rejectLaunch;
const pendingLaunch = new Promise(resolve => { rejectLaunch = resolve; });

(async () => {
  let portfolioDue = false;
  let smartControlSnapshot = null;
  const browser = await chromium.launch({channel:'chrome', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const req = route.request();
      const url = new URL(req.url());
      if (url.pathname.startsWith('/assets/')) {
        const file = path.basename(url.pathname);
        const asset = fs.readFileSync(path.join(dashboard,file));
        const body = file === 'app.js' ? asset.toString() + '\nwindow.renderTestScan = renderSmartScan;' : asset;
        return route.fulfill({contentType:file.endsWith('.css') ? 'text/css' : 'text/javascript',body});
      }
      if (!url.pathname.startsWith('/api/')) {
        return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(dashboard,'index.html'))});
      }
      const request = req.method() === 'POST' ? req.postDataJSON() : null;
      if (request) requests.push({path:url.pathname, request});
      let json = {};
      switch (url.pathname) {
        case '/api/smart-scan':
          if (request) {
            await pendingLaunch;
            return route.fulfill({status:409,json:{detail:'Fixture rejected the launch'}});
          }
          json = smartControlSnapshot || smart; break;
        case '/api/smart-scan/pause':
        case '/api/smart-scan/resume': {
          const status = url.pathname.endsWith('/pause') ? 'paused' : 'running';
          smartControlSnapshot = {...smart,id:'activity-fixture',status,control_status:status};
          json = smartControlSnapshot; break;
        }
        case '/api/smart-scan/inventory':
          json = smart; break;
        case '/api/web-scan/estimates':
          if (request.max_network_inspections === 1000) await hold.promise;
          json = {request, provider_cost_usd:request.max_network_inspections / 100};
          break;
        case '/api/discovery-methods':
          json = {capabilities:{openai:true,apify:true}, tabs:[
            {id:'portfolios', ready:true, sources:[{id:17,status:'accepted',scan_eligible:portfolioDue}]},
            {id:'directories', ready:true, sources:[]},
            {id:'searches', ready:false, sources:[{id:18,status:'accepted'}]},
            {id:'customer_lists', ready:true, sources:[{id:19,status:'accepted'}]},
          ]}; break;
        case '/api/discovery-methods/estimates':
          json = {estimate:{request, runnable:true, provider_cost_usd:0}}; break;
        case '/api/ad-discovery/estimates':
          json = {quote_id:'ad-quote', maximum_charge_usd:request.max_total_charge_usd,
            expires_at:new Date(Date.now()+60000).toISOString(), runnable:true}; break;
        case '/api/ad-discovery/overview':
          json = {apify_configured:true}; break;
        case '/api/microlink-discovery/estimate':
          json = {configured:true,max_requests:request.request_limit,estimate_id:'company-quote'}; break;
        case '/api/microlink-contacts/estimate':
          json = {configured:true,companies:request.company_count,max_requests:9,estimate_id:'contact-quote'}; break;
        case '/api/people-hydration/status':
          json = {configured:true}; break;
        case '/api/people-hydration/estimate':
          json = {maximum_usd:1,due_companies:request.company_limit}; break;
        case '/api/sales-engine/buyer-intelligence/quote':
          json = {id:1,status:'quoted',maximum_microusd:request.maximum_microusd,companies:1,requests_maximum:1}; break;
        case '/api/scans':
        case '/api/ad-discovery/runs':
          json = {id:1,job_id:'fixture',status:'running'}; break;
      }
      return route.fulfill({json});
    });
    await page.goto('http://scan-estimates.test/#smart_scan');
    const nextPost = (pathname, predicate = () => true) => page.waitForRequest(req =>
      req.method() === 'POST' && new URL(req.url()).pathname === pathname && predicate(req.postDataJSON()));
    const firstEstimate = nextPost('/api/web-scan/estimates');
    await page.locator('#scan-launch-trigger').click();
    await firstEstimate;
    const selectionStyle = locator => locator.evaluate(element => {
      const {backgroundColor, backgroundImage} = getComputedStyle(element);
      return {backgroundColor, backgroundImage};
    });
    const siteIntelligence = page.locator('#web-scan-form [name=free_intelligence]');
    const siteOption = siteIntelligence.locator('..');
    const selectedStyle = await selectionStyle(siteOption);
    assert.notEqual(selectedStyle.backgroundImage, 'none');
    assert.deepEqual(await selectionStyle(page.locator('#web-scan-form [name=agency_discovery]').locator('..')), selectedStyle);
    assert.deepEqual(await selectionStyle(page.locator('#smart-scan-due-hydration').locator('xpath=ancestor::div[contains(@class, "panel-head")]')), selectedStyle);
    await siteIntelligence.press('Space');
    assert.equal(await siteIntelligence.isChecked(), false);
    assert.equal((await selectionStyle(siteOption)).backgroundImage, 'none');
    assert.equal(await siteIntelligence.evaluate(input => getComputedStyle(input).outlineStyle), 'solid');
    await siteIntelligence.press('Space');
    assert.deepEqual(await selectionStyle(siteOption), selectedStyle);
    const changedEstimate = nextPost('/api/web-scan/estimates', request => request.max_network_inspections === 25);
    const sites = page.locator('#web-scan-form [name=max_network_inspections]');
    await sites.fill('20');
    await sites.fill('25');
    await changedEstimate;
    await page.locator('#run-web-scan').waitFor();
    hold.release();
    assert.match(await page.locator('#run-web-scan').textContent(), /\$0\.25/);
    assert.ok(!requests.some(row => row.path === '/api/scans'));
    await page.locator('#discovery-method-customer_lists form').waitFor();
    for (const id of ['portfolios','directories','searches','company_intelligence']) {
      assert.equal(await page.locator('#discovery-tab-' + id).isVisible(), false, id);
      assert.equal(await page.locator('#discovery-panel-' + id).isVisible(), false, id);
    }
    const customers = page.locator('#discovery-panel-customer_lists');
    assert.deepEqual(await selectionStyle(customers.locator('.discovery-source-choice')), selectedStyle);
    const ai = customers.locator('[name=ai_resolution]');
    const aiCalls = customers.locator('[name=max_ai_tool_calls]');
    assert.equal(await aiCalls.isVisible(), false);
    await ai.check();
    await aiCalls.waitFor();
    assert.deepEqual(await selectionStyle(ai.locator('..')), selectedStyle);
    await ai.uncheck();
    await aiCalls.waitFor({state:'hidden'});
    await page.locator('#discovery-tab-customer_lists').press('ArrowRight');
    assert.equal(await page.locator('#discovery-tab-public_graph').getAttribute('aria-selected'), 'true');
    const reloadCatalogue = async () => {
      await page.locator('#smart-scan-options-dialog').press('Escape');
      const loaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/discovery-methods');
      await page.locator('#find-directories-trigger').click();
      await loaded;
      await page.locator('#find-directories-dialog').press('Escape');
      await page.locator('#scan-launch-trigger').click();
    };
    portfolioDue = true;
    await reloadCatalogue();
    await page.locator('#discovery-tab-portfolios').click();
    await page.locator('#discovery-panel-portfolios [name=suggestion_ids]').waitFor();
    portfolioDue = false;
    await reloadCatalogue();
    await page.locator('#discovery-panel-portfolios').waitFor({state:'hidden'});
    assert.equal(await page.locator('#discovery-tab-web_ct').getAttribute('aria-selected'), 'true');
    await page.locator('#discovery-tab-public_graph').click();
    await page.locator('[data-start-discovery-method=public_graph]').waitFor();
    await page.locator('#discovery-tab-paid_ads').click();
    const paid = page.locator('#start-paid-discovery');
    await paid.waitFor();
    assert.equal(await paid.isDisabled(), true);
    await page.locator('#approve-paid-cap').check();
    assert.deepEqual(await selectionStyle(page.locator('#approve-paid-cap').locator('..')), selectedStyle);
    const updatedAds = nextPost('/api/ad-discovery/estimates', request => request.max_total_charge_usd === 5);
    await page.locator('#paid-discovery-form [name=max_total_charge_usd]').fill('5');
    await updatedAds;
    await paid.waitFor();
    assert.equal(await paid.isDisabled(), true);
    assert.match(await paid.textContent(), /\$5\.00/);
    assert.ok(!requests.some(row => row.path.endsWith('/runs')));
    await page.keyboard.press('Escape');
    for (const mode of ['discovery','contacts']) {
      await page.locator('[data-scan-setup='+mode+']').click();
      const start = page.locator('#scan-setup-start');
      await page.waitForFunction(() => !document.querySelector('#scan-setup-start').disabled);
      assert.match(await start.textContent(), /up to [\d,]+ requests/);
      await page.keyboard.press('Escape');
    }
    const smartLaunch = nextPost('/api/smart-scan');
    await page.locator('#scan-launch-trigger').click();
    await page.locator('#start-smart-scan').click();
    await smartLaunch;
    assert.equal(await page.locator('#smart-scan-options-dialog').isVisible(), false);
    assert.match(await page.locator('#scan-launch-trigger').textContent(), /Starting Smart Scan/);
    const startingPills = page.locator('#scan-overview .scan-overview-item');
    assert.equal(await startingPills.count(), 1);
    assert.match(await startingPills.textContent(), /Smart Scan.*0%/);
    for (const selector of ['.scan-shortcuts', '.smart-scan-choice-divider', '#scan-launch-lock']) {
      assert.equal(await page.locator(selector).isVisible(), false, selector);
    }
    for (const selector of ['#pause-resume-smart-scan', '#stop-smart-scan']) {
      const control = page.locator(selector);
      assert.equal(await control.isVisible(), true, selector);
      assert.equal(await control.isDisabled(), true, selector);
    }
    assert.match(await page.locator('#smart-scan-phases li.active').textContent(), /Quantify backlog.*Preparing/);
    rejectLaunch();
    await page.locator('.scan-shortcuts').waitFor();
    assert.equal(await page.locator('#smart-scan-run-controls').isVisible(), false);
    const activity = page.locator('#smart-terminal-lines .is-current-activity');
    await page.evaluate(snapshot => {
      window.testScan = {...snapshot,id:'activity-fixture',status:'running',events:[
        {message:'Searching for new company sources',timestamp:'2026-11-10T21:37:57Z'},
        {message:'Older hydration progress',timestamp:'2026-11-10T21:37:49Z'},
        {message:'A failed source',level:'error',timestamp:'2026-11-10T21:37:58Z'},
      ],actions:[{id:1,message:'Hydrating company',timestamp:'2026-11-10T21:37:53Z'}]};
      window.renderTestScan(window.testScan);
    }, smart);
    assert.equal(await activity.count(), 1);
    assert.match(await activity.textContent(), /Searching for new company sources/);
    assert.ok(await activity.locator('strong').evaluate(element => {
      const expected = document.createElement('span');
      expected.style.color = 'var(--success)';
      element.append(expected);
      const green = getComputedStyle(expected).color;
      expected.remove();
      return getComputedStyle(element).color === green;
    }));
    await page.evaluate(() => {
      window.testScan.actions.push({id:2,message:'Refreshing the next company',timestamp:'2026-11-10T21:38:00Z'});
      window.renderTestScan(window.testScan);
    });
    await activity.filter({hasText:'Refreshing the next company'}).waitFor();
    assert.equal(await activity.count(), 1);
    await page.locator('#smart-terminal-errors-tab').click();
    assert.equal(await activity.count(), 0);
    await page.locator('#smart-terminal-activity-tab').click();
    assert.match(await activity.textContent(), /Refreshing the next company/);
    const toggle = page.locator('#pause-resume-smart-scan');
    assert.equal(await page.locator('#smart-scan-run-controls button').count(), 2);
    assert.equal(await toggle.textContent(), 'Pause');
    const original = await toggle.elementHandle();
    const pause = nextPost('/api/smart-scan/pause');
    await toggle.click();
    await pause;
    await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
    assert.ok(await original.evaluate(button => button === document.querySelector('#pause-resume-smart-scan')));
    const resume = nextPost('/api/smart-scan/resume');
    await toggle.click();
    await resume;
    await page.getByRole('button',{name:'Pause',exact:true}).waitFor();
    smartControlSnapshot = null;
    await page.evaluate(snapshot => window.renderTestScan(snapshot), smart);
    await page.locator('[data-view=settings]').click();
    await page.locator('#open-treg-settings').click();
    await page.locator('#buyer-intelligence-form [name=company_limit]').fill('12');
    await page.locator('#buyer-intelligence-status button').waitFor();
    await page.locator('#people-hydration-form [name=company_limit]').fill('8');
    await page.locator('#start-people-hydration').waitFor();
    assert.equal(await page.getByRole('button',{name:'Estimate cost',exact:true}).count(),0);
    assert.ok(!requests.some(row => row.path === '/api/scans' || row.path.endsWith('/runs')));
    await page.keyboard.press('Escape');
    await page.locator('[data-view=smart_scan]').click();
    await page.locator('#scan-launch-trigger').click();
    await page.locator('#discovery-tab-paid_ads').click();
    await page.locator('#approve-paid-cap').check();
    const launch = nextPost('/api/ad-discovery/runs');
    await paid.click();
    assert.deepEqual((await launch).postDataJSON(), {
      quote_id:'ad-quote',confirmed:true,confirmed_max_usd:5,
    });
    assert.deepEqual(errors,[]);
    console.log('Automatic quotes, stale draft rejection, cap approval reset, and explicit launch gating passed');
  } finally {
    hold.release();
    rejectLaunch();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
